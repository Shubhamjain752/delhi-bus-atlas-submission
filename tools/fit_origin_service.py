"""Fit route-start movement and platform-gap history from audited October traversal labels."""
import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
import csv
import gzip
import hashlib
import json
from pathlib import Path
import sys
import tempfile

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from transit.router import IST, Router, approximate_m, coords

START = date(2026, 10, 1)
SPLIT = date(2026, 10, 25)
END = date(2026, 11, 1)
BASE = 1 << 24
SHARDS = 24
RECORD = np.dtype([("edge", "<i8"), ("band", "u1"), ("day", "u1"), ("gap", "<u2"),
                   ("origin", "u1"), ("holdout_eligible", "u1")])


def midnight(day):
    return int(datetime.combine(day, time(), IST).timestamp())


def edge_key(route, a, b):
    route, a, b = int(route), int(a), int(b)
    if not (0 <= a < BASE and 0 <= b < BASE and 0 <= route < 2**63//BASE**2):
        raise ValueError(f"GTFS edge identifiers exceed the signed 64-bit key range: {(route,a,b)}")
    return (route*BASE + a)*BASE + b


def name(key, band):
    combined, b = divmod(int(key), BASE)
    route, a = divmod(combined, BASE)
    return f"{route}|{a}|{b}|{'weekend' if band>=12 else 'weekday'}|{band%12}"


def origin_index(router, active):
    """An observed edge can identify a start only if it cannot be an intermediate movement."""
    starts, interior = set(), set()
    platform = set()
    for p in router.patterns:
        if p.service not in active:
            continue
        first = next((i for i,v in enumerate(p.pickups) if v==0), None)
        multiplicity = Counter(p.stops)
        for i,(a,b) in enumerate(zip(p.stops,p.stops[1:])):
            key = edge_key(p.route,a,b)
            if i != first or multiplicity[a]!=1 or multiplicity[b]!=1:
                interior.add(key)
            if (p.pickups[i] or a==b or multiplicity[a]!=1 or multiplicity[b]!=1
                    or approximate_m(coords(router.stops[a]),coords(router.stops[b]))<=120):
                continue
            platform.add(key)
            if i==first:
                starts.add(key)
    return starts-interior, platform


def schedule(router, day):
    """First public pickup, next stop, nominal civil timestamp and trip for runs on that civil date."""
    beginning = midnight(day)
    for service in (day - timedelta(days=1), day):
        active = router._active(service)
        base = midnight(service)
        for p in router.patterns:
            if p.service not in active:
                continue
            first = next((i for i,v in enumerate(p.pickups) if v==0), None)
            if first is None or first+1>=len(p.stops):
                continue
            for start,trip in zip(p.starts,p.trips):
                clock = base + start + p.departures[first]
                if beginning <= clock < beginning+86400:
                    yield p.route,p.stops[first],p.stops[first+1],clock,trip


def days():
    return [START+timedelta(days=i) for i in range((END-START).days)]


def load(labels, day):
    path = labels / f"{day}.npz"
    meta = json.loads(path.with_suffix(".json").read_bytes())
    if meta["method"]!="delhi-directed-traversals-v1" or meta["day"]!=str(day):
        raise ValueError(f"Invalid cache receipt for {day}")
    with np.load(path,allow_pickle=False) as source:
        a = {k:source[k] for k in ("route","from_stop","to_stop","start","end","received")}
    n = len(a["start"])
    if n!=meta["labels"] or any(len(x)!=n for x in a.values()):
        raise ValueError(f"Incomplete cache for {day}")
    for col,limit in (("route",2**63//BASE**2),("from_stop",BASE),("to_stop",BASE)):
        if np.any((a[col]<0)|(a[col]>=limit)):
            raise ValueError(f"{day} {col} exceeds the packed edge key range")
    edge = (a["route"].astype("i8")*BASE+a["from_stop"])*BASE+a["to_stop"]
    return np.rec.fromarrays([edge,a["start"],a["end"],a["received"]],names="edge,start,end,received")


def dump_profile(path, profile):
    with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as output:
        temporary=Path(output.name)
    try:
        with temporary.open("wb") as raw:
            with gzip.GzipFile(filename="",fileobj=raw,mode="wb",compresslevel=5,mtime=0) as compressed:
                compressed.write(json.dumps(profile,separators=(",",":"),sort_keys=True).encode("utf-8"))
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def extract(router, labels, work, origin_events):
    schedules = [Counter(),Counter()]
    eligibility = {}
    next_day = load(labels, START)
    with gzip.open(origin_events,"wt",newline="",encoding="utf-8") as output:
        writer=csv.writer(output)
        writer.writerow(["route","origin","second","start","received","collision","mature"])
        for day in days():
            begin = midnight(day)
            following = load(labels,day+timedelta(days=1)) if day+timedelta(days=1)<END else next_day[:0]
            records = np.concatenate((next_day[next_day.start>=begin],following[following.start<begin+86400])).view(np.recarray)
            next_day = following
            active = frozenset(router._active(day) | router._active(day-timedelta(days=1)))
            if active not in eligibility:
                eligibility[active] = origin_index(router,active)
            origin, platform = eligibility[active]
            records=records[np.isin(records.edge,list(platform)) & (records.end+90<midnight(END))
                            & (records.received<midnight(END))]
            band = (records.start-begin)//7200 + 12*(day.weekday()>=5)
            frame=pd.DataFrame({"edge":records.edge,"start":records.start,"received":records.received,
                                "mature":np.maximum(records.end+90,records.received),"band":band})
            # Collapse identical seconds to a collision marker. Neither touching interval is scored.
            events=frame.groupby(["edge","band","start"],sort=True).agg(
                received=("received","max"),mature=("mature","max"),n=("received","size")).reset_index()
            events["ok"]=events.n==1
            adjacent=events.edge.eq(events.edge.shift()) & events.band.eq(events.band.shift())
            gap=events.start.diff().where(adjacent & events.ok & events.ok.shift(fill_value=False),0).fillna(0)
            if gap.max()>7200 or gap.min()<0:
                raise ValueError("Out-of-band interval")
            origin_mask=events.edge.isin(origin)
            for e in events[origin_mask].itertuples():
                combined,second=divmod(e.edge,BASE)
                route,first=divmod(combined,BASE)
                writer.writerow((route,first,second,e.start,e.received,int(not e.ok),e.mature))
            for route,a,b,t,trip in schedule(router,day):
                edge=edge_key(route,a,b)
                if edge in origin:
                    k=edge,(t-begin)//7200 + 12*(day.weekday()>=5)
                    schedules[1][k]+=1
                    if day<SPLIT:
                        schedules[0][k]+=1
            packed=np.empty(len(events),dtype=RECORD)
            packed["edge"],packed["band"],packed["day"]=events.edge,events.band,day.day
            packed["gap"]=gap
            packed["origin"]=origin_mask & events.ok
            # Cutoff-day records whose evidence arrives later cannot train the earlier fit.
            # An affected preceding event also disqualifies a spanning gap.
            eligible=events.mature<midnight(SPLIT)
            packed["holdout_eligible"]=eligible.astype("u1")+2*(eligible & (gap.eq(0) | eligible.shift(fill_value=False))).astype("u1")
            sectors=(packed["edge"]//(BASE*BASE))%SHARDS
            for s in range(SHARDS):
                with (work/f"shard-{s}").open("ab") as f:
                    packed[sectors==s].tofile(f)
            print(day,len(events),int(origin_mask.sum()),flush=True)
    return schedules


def fit(work, schedules, digest, cut):
    cutoff = SPLIT if cut else END
    buckets={}
    for (edge,band),n in schedules.items():
        buckets[name(edge,band)]={"scheduled":n,"observed":0,"observed_days":0,"gap_count":0,"gap_days":0,
                                  "median_gap_seconds":None}
    platform_gaps={}
    for s in range(SHARDS):
        df=pd.DataFrame.from_records(np.fromfile(work/f"shard-{s}",dtype=RECORD))
        if cut:
            df=df[df.holdout_eligible.gt(0)]
        origins=df[df.origin.eq(1)].groupby(["edge","band"]).day.agg(["size","nunique"])
        for (edge,band),n,d in origins.itertuples():
            k=name(edge,band)
            row=buckets.setdefault(k,{"scheduled":0,"gap_count":0,"gap_days":0,"median_gap_seconds":None})
            row.update(observed=n,observed_days=d)
        valid=df[df.gap.gt(0)]
        if cut:
            valid=valid[valid.holdout_eligible.eq(3)]
        # An edge can serve as an origin on only some dates, depending on active patterns.
        origin_gaps=valid[valid.origin.eq(1)].groupby(["edge","band"]).agg(n=("gap","size"),days=("day","nunique"),g=("gap","median"))
        for (edge,band),n,d,g in origin_gaps.itertuples():
            buckets[name(edge,band)].update(gap_count=n,gap_days=d,median_gap_seconds=round(g) if n>=5 and d>=3 else None)
        grouped=valid.groupby(["edge","band"])
        counts=grouped.agg(n=("gap","size"),d=("day","nunique"),median=("gap","median"))
        counts=counts[counts.n.ge(5) & counts.d.ge(3)].copy()
        counts["p90"]=grouped.gap.quantile(.9)
        for (edge,band),n,d,g,p in counts.itertuples():
            platform_gaps[name(edge,band)]=[n,d,round(g),round(p)]
    return {"version":1,"feed_sha256":digest,"available_from":str(cutoff),"training_start":str(START),
            "training_end":str(cutoff-timedelta(days=1)),"buckets":buckets,"platform_gaps":platform_gaps}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--labels",type=Path,required=True)
    p.add_argument("--work",type=Path,required=True,help="Scratch directory on a volume with 1 GB free")
    p.add_argument("--feed",type=Path,default=ROOT/"delhi_buses_static_gtfs_v1.zip")
    p.add_argument("--output",type=Path,default=ROOT/"data/origin-service-history.json.gz")
    args=p.parse_args()
    args.work.mkdir(exist_ok=True,parents=True)
    with args.feed.open("rb") as source:
        digest=hashlib.file_digest(source,"sha256").hexdigest()
    reference=json.loads((ROOT/"data/october-eta-comparison.json").read_bytes())
    if digest!=reference["source"]["gtfs_sha256"]:
        raise ValueError("The October GPS labels were matched to another timetable")
    router=Router(args.feed,ROOT/"data",None,None)
    with tempfile.TemporaryDirectory(dir=args.work,prefix="gap-shards-") as scratch:
        scratch=Path(scratch)
        schedules=extract(router,args.labels,scratch,args.work/"origin-service-events.csv.gz")
        for cut,counts,path in ((True,schedules[0],args.work/"origin-service-holdout.json.gz"),
                                (False,schedules[1],args.output)):
            profile=fit(scratch,counts,digest,cut)
            dump_profile(path,profile)
            print(path,len(profile["buckets"]),len(profile["platform_gaps"]),flush=True)


if __name__=="__main__":
    main()
