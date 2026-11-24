"""Score historical spacing on future recorded movements. No raw vehicle identity is required."""
import argparse
from bisect import bisect_left
from collections import Counter, defaultdict
import csv
from datetime import date, datetime, timedelta
import gzip
import hashlib
import json
from pathlib import Path
import sys
from statistics import median

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from transit.router import IST, Router

SPLIT = date(2026, 10, 25)
END = date(2026, 11, 1)


def midnight(d):
    return int(datetime(d.year, d.month, d.day, tzinfo=IST).timestamp())


def profile_type(d):
    return "weekend" if d.weekday() >= 5 else "weekday"


def bucket(edge, daytype, band):
    return "|".join((*map(str, edge), daytype, str(band)))


def intervals(events):
    """Keep both sides inside the cohort; a simultaneous duplicate interrupts both adjoining gaps."""
    ordered = sorted(events)
    return [(a,b) for (a,ca),(b,cb) in zip(ordered,ordered[1:]) if not ca and not cb and b>a]


def origin_waits(events, training, router):
    from tools.fit_origin_service import schedule

    by_day = defaultdict(lambda:defaultdict(list))
    with gzip.open(events, "rt", encoding="utf-8", newline="") as stream:
        for raw in csv.DictReader(stream):
            t = int(raw["start"])
            d = datetime.fromtimestamp(t,IST).date()
            cutoff = midnight(SPLIT if d<SPLIT else END)
            edge = tuple(int(raw[k]) for k in ("route","origin","second"))
            band = (t-midnight(d))//7200
            ambiguous = bool(int(raw["collision"])) or int(raw["mature"]) >= cutoff
            by_day[d][edge,band].append((t,ambiguous))
    exposure = defaultdict(lambda:[0,0,0])
    for d, groups in by_day.items():
        if d>=SPLIT:
            continue
        for (edge,band), obs in groups.items():
            k = bucket(edge,profile_type(d),band)
            for a,b in intervals(obs):
                n = b-a
                s = exposure[k]
                s[0] += n*n
                s[1] += n
                s[2] += 1
    sums = defaultdict(Counter)
    coverage = Counter()
    gaps = defaultdict(Counter)
    recorded_starts = defaultdict(Counter)
    for i in range((END-SPLIT).days):
        d = SPLIT+timedelta(days=i)
        lo = midnight(d)
        groups = defaultdict(list)
        for route,origin,second,t,_ in schedule(router,d):
            if lo<=t<lo+86400:
                groups[(int(route),int(origin),int(second)),(t-lo)//7200].append(t)
        for (edge,band), departures in groups.items():
            planned = sorted(departures)
            key = bucket(edge,profile_type(d),band)
            w = training["buckets"].get(key,{})
            gap = w.get("median_gap_seconds")
            qualified = gap is not None and w.get("gap_count",0)>=5 and w.get("gap_days",0)>=3
            if not w:
                continue
            records = by_day[d].get((edge,band),())
            found=sum(not ambiguous for _,ambiguous in records)
            category="previously_observed" if w["observed"] else "no_observed_starts_in_training"
            counts=recorded_starts[category]
            counts["scheduled_band_dates"] += 1
            counts["with_one_or_more_observed_starts"] += bool(found)
            counts["scheduled_starts"] += len(planned)
            counts["observed_start_movements"] += found
            pairs = intervals(records)
            distinct_planned=sorted(set(planned))
            planned_gaps=[b-a for a,b in zip(distinct_planned,distinct_planned[1:])]
            if qualified and planned_gaps:
                nominal=median(planned_gaps)
                for a,b in pairs:
                    for g in ("all",profile_type(d),*(["00_to_04"] if band<2 else [])):
                        gp=gaps[g]
                        gp["scored"]+=1
                        gp["timetable"]+=abs(b-a-nominal)
                        gp["historical"]+=abs(b-a-gap)
            j = 0
            for t in range(lo+band*7200,lo+(band+1)*7200,900):
                coverage["scheduled_origin_band_quarter_hours"] += 1
                while j<len(pairs) and pairs[j][1]<t:
                    j += 1
                if j==len(pairs) or pairs[j][0]>=t:
                    continue
                coverage["bracketed_by_observed_movements"] += 1
                idx = bisect_left(planned,t)
                if idx==len(planned):
                    continue
                coverage["also_has_next_scheduled_start_in_band"] += 1
                if not qualified:
                    continue
                x,den,_ = exposure[key]
                if not den:
                    raise ValueError("Missing training intervals for supported origin")
                if exposure[key][2] != w["gap_count"]:
                    raise ValueError(f"Training gaps disagree for {key}")
                target = pairs[j][1]-t
                estimates = {"timetable":planned[idx]-t,"half_historical_median":gap/2,
                             "length_weighted_history":x/(2*den)}
                slices = ["all",profile_type(d)]
                if band<2:
                    slices.append("00_to_04")
                for name in slices:
                    agg = sums[name]
                    agg["queries"] += 1
                    agg["observed_wait"] += target
                    for m,v in estimates.items():
                        agg[m] += abs(v-target)
    gap_results={k:{"scored":v["scored"],"timetable_mae_seconds":round(v["timetable"]/v["scored"],2),
                    "historical_mae_seconds":round(v["historical"]/v["scored"],2)} for k,v in gaps.items()}
    return {"coverage":dict(coverage),"origin_gap_comparison":gap_results,
            "observed_starts_by_training_support":dict(recorded_starts),"mean_seconds":{
        group:{"queries":v["queries"],**{k:round(s/v["queries"],2) for k,s in v.items() if k!="queries"}}
        for group,v in sums.items()}}


def platform_gaps(labels, trained, router):
    from tools.fit_origin_service import origin_index, BASE

    ids=np.array(sorted(map(int,router.stops)),dtype=np.int64)
    slot={int(s):i for i,s in enumerate(ids)}
    def packed(r,a,b,h):
        return ((r*len(ids)+a)*len(ids)+b)*12+h
    eligible = {}
    indexes = {}
    for typ in ("weekday","weekend"):
        items = []
        for key,value in trained["platform_gaps"].items():
            r,a,b,t,h = key.split("|")
            if t==typ and value[0]>=5 and value[1]>=3:
                if int(a) not in slot or int(b) not in slot:
                    raise ValueError(f"Historical key contains a stop absent from the timetable: {key}")
                items.append((packed(int(r),slot[int(a)],slot[int(b)],int(h)),value[2],value[3]))
        matrix = np.asarray(sorted(items),dtype=np.int64).reshape(-1,3)
        indexes[typ] = (matrix[:,0],matrix[:,1],matrix[:,2])
    metrics = defaultdict(Counter)
    carry = None
    for i in range((END-SPLIT).days):
        d = END-timedelta(days=i+1)
        lo = midnight(d)
        with np.load(labels/(d.isoformat()+".npz")) as arrays:
            t = arrays["start"]
            if np.any(arrays["end"]<t) or np.any(arrays["received"]<arrays["end"]):
                raise ValueError("Invalid observation clocks")
            valid=(arrays["received"]<midnight(END)) & (arrays["end"]+90<midnight(END))
            r,a,b=(arrays[k][valid].astype(np.int64) for k in ("route","from_stop","to_stop"))
            ai,bi=np.searchsorted(ids,a),np.searchsorted(ids,b)
            if len(a) and (ai.max()>=len(ids) or bi.max()>=len(ids)
                           or not np.array_equal(ids[ai],a) or not np.array_equal(ids[bi],b)):
                raise ValueError("Unexpected stop or route identifier")
            t=t[valid]
            k=packed(r,ai,bi,((t-lo)%86400)//7200)
        previous_day=t<lo
        for_next=(k[previous_day],t[previous_day])
        k,t=k[~previous_day],t[~previous_day]
        if carry is not None:
            k,t=np.concatenate((k,carry[0])),np.concatenate((t,carry[1]))
        carry=for_next
        active=frozenset(router._active(d)|router._active(d-timedelta(days=1)))
        if active not in eligible:
            _,platforms=origin_index(router,active)
            eligible[active]=[packed(e//(BASE*BASE),slot[(e//BASE)%BASE],slot[e%BASE],0)//12 for e in platforms]
        eligible_row=np.isin(k//12,eligible[active])
        k,t=k[eligible_row],t[eligible_row]
        order=np.lexsort((t,k))
        k,t=k[order],t[order]
        duplicate=(k[1:]==k[:-1]) & (t[1:]==t[:-1])
        ambiguous=np.r_[False,duplicate] | np.r_[duplicate,False]
        valid_pair=(k[1:]==k[:-1]) & ~ambiguous[1:] & ~ambiguous[:-1]
        keys,actual=k[1:][valid_pair],np.diff(t)[valid_pair]
        trained_keys,medians,upper=indexes[profile_type(d)]
        lookup=np.searchsorted(trained_keys,keys)
        chosen=lookup<len(trained_keys)
        chosen[chosen] &= trained_keys[lookup[chosen]]==keys[chosen]
        for g,mask in (("all",np.ones(len(keys),dtype=bool)),(profile_type(d),np.ones(len(keys),dtype=bool)),
                       ("00_to_04",keys%12<2)):
            matched=mask & chosen
            v=metrics[g]
            v["observed_gaps"]+=int(mask.sum())
            v["scored"]+=int(matched.sum())
            ix=lookup[matched]
            delta=actual[matched]-medians[ix]
            v["absolute_error"]+=int(np.abs(delta).sum())
            v["at_or_below_median"]+=int((delta<=0).sum())
            v["at_or_below_p90"]+=int((actual[matched]<=upper[ix]).sum())
    return {k:{**dict(v), "median_gap_mae_seconds":round(v["absolute_error"]/v["scored"],2),
               "within_historical_p90_pct":round(100*v["at_or_below_p90"]/v["scored"],2)}
            for k,v in metrics.items() if v["scored"]}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--training",type=Path,required=True)
    parser.add_argument("--origin-events",type=Path,required=True)
    parser.add_argument("--labels",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    with gzip.open(args.training,"rt",encoding="utf-8") as f:
        model=json.load(f)
    if model["training_end"]!="2026-10-24":
        raise ValueError("Held-out tests require the 1 to 24 October training fit")
    router=Router(ROOT/"delhi_buses_static_gtfs_v1.zip",ROOT/"data",None,None)
    out={"training":"2026-10-01 to 2026-10-24","test":"2026-10-25 to 2026-10-31",
         "origin_wait_proxy":origin_waits(args.origin_events,model,router)}
    out["training_file_sha256"]=hashlib.sha256(args.training.read_bytes()).hexdigest()
    print("Origin tests:",out["origin_wait_proxy"]["mean_seconds"],flush=True)
    out["platform_spacing"]=platform_gaps(args.labels,model,router)
    args.output.write_text(json.dumps(out,indent=2)+"\n",encoding="utf-8")


if __name__=="__main__":
    main()
