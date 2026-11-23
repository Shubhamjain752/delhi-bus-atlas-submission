"""Replay first direct-bus recommendations with receipt-timed Delhi GPS.

Prepare stages never use observed outcomes to select pairs. The score stage
runs the two production policies against the exact same pre-issue messages.
"""
import argparse
from bisect import bisect_left
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from functools import lru_cache
import gzip
import hashlib
import json
from itertools import groupby
from pathlib import Path
import sys

import numpy as np
from scipy.spatial import cKDTree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.fleet_gps import reader
from tools.train_eta import boundary, format_clock, open_db
from transit.eta import EtaModel, IST
from transit.live import protobuf
from transit.replay import CapturedOverlay
from transit.router import Router, approximate_m, coords


ISSUE_MIN = 10
PAIRS_PER_HOUR = 12
HORIZON = 30 * 60
DATES = ("2026-10-22", "2026-10-23", "2026-10-24", "2026-10-25", "2026-10-26", "2026-10-29")


def write(path, data):
    draft = path.with_suffix(".pending")
    with gzip.open(draft, "wt", encoding="utf-8", compresslevel=1) as stream:
        json.dump(data, stream, separators=(",", ":"), allow_nan=False)
    draft.replace(path)


def read(path):
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        return json.load(stream)


def scheduled_panel(router, day, pairs_per_hour=PAIRS_PER_HOUR):
    """Choose different parts of Delhi solely from GTFS before looking at GPS."""
    slots = [defaultdict(dict) for _ in range(24)]
    for offset in (-1, 0):
        active = router._active(day + timedelta(days=offset))
        for p in router.patterns:
            if p.service not in active:
                continue
            for i, pair in enumerate(zip(p.stops, p.stops[1:])):
                if p.pickups[i] or p.drops[i+1] or pair[0] == pair[1]:
                    continue
                if approximate_m(coords(router.stops[pair[0]]), coords(router.stops[pair[1]])) < 150:
                    continue
                delta = p.departures[i] + offset * 86400
                first = max(0, (p.starts[0] + delta - HORIZON) // 3600)
                last = min(23, (p.starts[-1] + delta) // 3600)
                for h in range(first, last + 1):
                    lower = h * 3600 + ISSUE_MIN * 60 - delta
                    count = bisect_left(p.starts, lower + HORIZON) - bisect_left(p.starts, lower)
                    if count:
                        line = router.routes[p.route][0]
                        by_line = slots[h][pair]
                        by_line[line] = by_line.get(line, 0) + count
    panel = []
    for h, pairs in enumerate(slots):
        candidates = [(pair, lines) for pair, lines in pairs.items() if len(lines) >= 2]
        # Spread the busiest shared links across ~4-km cells. Half the slots use
        # a deterministic draw from the remaining cells, including quieter links.
        def draw(pair):
            return hashlib.sha256(f"{day}/{h}/{pair[0]}/{pair[1]}".encode()).digest()
        dense = sorted(candidates, key=lambda v:(-min(10,len(v[1])), -min(30,sum(v[1].values())), draw(v[0])))
        spread = sorted(candidates, key=lambda v:draw(v[0]))
        chosen, cells = [], set()
        for ordered, cap in ((dense, pairs_per_hour // 2), (spread, pairs_per_hour)):
            for pair, lines in ordered:
                if len(chosen) >= cap:
                    break
                a = router.stops[pair[0]]
                cell = int(a["lat"]*25), int(a["lon"]*22)
                if cell in cells:
                    continue
                chosen.append(pair)
                cells.add(cell)
        if pairs_per_hour==0:
            chosen=sorted(pair for pair,_ in candidates)
        # Include all possible direct route IDs at the two platforms for the
        # forecast and truth, even when only the panel selection used adjacency.
        hour = []
        for a,b in chosen:
            end = {pid for pid,j in router.at[b] if not router.patterns[pid].drops[j]}
            ids = sorted({router.patterns[pid].route for pid,i in router.at[a]
                          if pid in end and not router.patterns[pid].pickups[i]
                          and any(s==b and not router.patterns[pid].drops[j]
                                  for j,s in enumerate(router.patterns[pid].stops) if j>i)})
            hour.append([a,b,ids])
        panel.append(hour)
    return panel


def issue_stamp(day, h):
    return int(datetime.combine(day, time(h, ISSUE_MIN), IST).timestamp())


def past_rows(db, lo, hi, ids):
    sql = ("SELECT time, timestamp, vehicle_id, trip_id, route_id, lat, lng FROM vehicle_feed "
           "WHERE rowid>=? AND rowid<? AND vehicle_id IS NOT NULL AND route_id IS NOT NULL")
    rows = list(db.execute(sql,(lo,hi)))
    relevant = {r[2] for r in rows if str(int(r[4])) in ids}
    # Keep same-vehicle claims on other routes, including conflicts within one poll.
    return [[t, int(o), v, run or "", str(int(rt)), float(lat), float(lon)]
            for t,o,v,run,rt,lat,lon in rows if v in relevant and o and lat and lon]


def witnessed(router, cases, fixes, issue):
    """Score only ordered, same-vehicle same-trip passages near both poles."""
    if not cases:
        return []
    stop_ids = sorted({s for a,b,_ in cases for s in (a,b)})
    # Bounding search only: final inclusion uses the production distance formula.
    poles = np.array([coords(router.stops[s]) for s in stop_ids])
    tree = cKDTree(poles * [110574, 97000])
    distances, _ = tree.query(np.column_stack((fixes["lat"]*110574, fixes["lon"]*97000)), k=1)
    nearby = fixes[distances<=47]
    targets = defaultdict(set)
    for a,b,routes in cases:
        for s in (a,b):
            targets[s].update(map(int,routes))
    seen = defaultdict(lambda:defaultdict(set))
    # A close fix may fall inside two poles. Reject indistinguishable poles for
    # that route, exactly as vehicle-to-platform matching does for live inputs.
    unique = np.unique(nearby[["bus","trip","route","obs","lat","lon"]])
    for row in unique:
        lat,lon,route = float(row["lat"]),float(row["lon"]),int(row["route"])
        hits = [(dist,s) for dist,s in router._nearby((lat,lon),45)
                if any(router.patterns[pid].route==str(route) for pid,_ in router.at[s])]
        if not hits or len(hits)>1 and hits[1][0]-hits[0][0]<12:
            continue
        s = hits[0][1]
        if route in targets[s]:
            seen[s][(route,int(row["bus"]),int(row["trip"]))].add(int(row["obs"]))
    output = []
    for a,b,routes in cases:
        route_ids=set(map(int,routes))
        complete = {}
        unknown = []
        for vehicle, times in seen[a].items():
            rt, _, _ = vehicle
            if rt not in route_ids:
                continue
            times = sorted(times)
            groups = [[times[0]]]
            for t in times[1:]:
                if t-groups[-1][-1]>120:
                    groups.append([])
                groups[-1].append(t)
            ends = sorted(seen[b].get(vehicle, ()))
            for visit in groups:
                board = visit[0]
                if board < issue or board > issue+HORIZON:
                    continue
                line = router.routes[str(rt)][0]
                j = bisect_left(ends,board+10)
                if j==len(ends) or ends[j] - visit[-1] > 600:
                    unknown.append([line,board])
                    continue
                record = [board,ends[j]]
                # A following vehicle on the same line can reach the exit first.
                # Every board and exit time is backed by a complete same-bus run.
                existing=complete.get(line,record)
                complete[line]=[min(earlier,current) for earlier,current in zip(existing,record)]
        output.append({"complete":complete,"without_exit":unknown})
    return output


def prepare(day, dbfile, scratch, router, pairs_per_hour=PAIRS_PER_HOUR):
    d=date.fromisoformat(day)
    cache=scratch/day
    cache.mkdir(exist_ok=True,parents=True)
    panel_path = cache/"panel.gz"
    if not panel_path.exists():
        write(panel_path,scheduled_panel(router,d,pairs_per_hour))
    panel=read(panel_path)
    scan=reader()
    midnight=int(datetime.combine(d,time(),IST).timestamp())
    with open_db(dbfile) as db:
        for h,cases in enumerate(panel):
            output=cache/f"{h:02}.gz"
            previous=read(output) if output.exists() else None
            if previous is not None and previous.get("truth_version")==2:
                continue
            at=h*3600+ISSUE_MIN*60
            start=boundary(db,format_clock(at-240))
            split=boundary(db,format_clock(at+1))
            end=boundary(db,format_clock(at+HORIZON+600+1))
            ids={s for _,_,rr in cases for s in rr}
            pre=previous["past"] if previous is not None else past_rows(db,start,split,ids)
            # Include pre-issue presence to exclude buses already standing there.
            _,_,fixes=scan(dbfile,start,end,midnight,h,end-start)
            out=witnessed(router,cases,fixes,midnight+at)
            write(output,{"truth_version":2,"hour":h,"cases":cases,"past":pre,"witnesses":out,
                          "raw_rows":end-start,"valid_rows":len(fixes)})
            print(day,h,"prepared",len(cases),len(pre),sum(bool(w["complete"]) for w in out),flush=True)


def restore(registry, model, midnight, issue, past):
    overlay=CapturedOverlay(model, ROOT/"data", issue)
    overlay.router,overlay.ids,overlay.by_clock,overlay.edges=(
        registry.router,registry.ids,registry.by_clock,registry.edges)
    for clock,rows in groupby(past,key=lambda r:r[0]):
        hh,mm,ss=map(int,clock.split(":"))
        received=midnight+hh*3600+mm*60+ss
        assert received<=issue
        feed=protobuf().FeedMessage()
        feed.header.gtfs_realtime_version="2.0"
        feed.header.timestamp=received
        for _,observed,vehicle,run,route,lat,lon in dict.fromkeys(tuple(r) for r in rows):
            if observed>received:
                continue
            ent=feed.entity.add()
            ent.id=vehicle
            v=ent.vehicle
            v.vehicle.id=vehicle
            v.trip.route_id=route
            v.trip.trip_id=run
            v.timestamp=observed
            v.position.latitude=lat
            v.position.longitude=lon
        overlay.apply_positions(feed,received)
    # All pairs at this clock see an immutable, already-restored city snapshot.
    # Cache the pure identity resolution shared by those exact production calls.
    overlay.journey_positions=lru_cache(maxsize=1)(overlay.journey_positions)
    return overlay


def pick(router,a,b,when,overlay,enabled):
    options=router.journeys(a,b,when,asof=when,eta=overlay,max_transfers=0,
                            max_walk_m=0,limit=1,use_position_board_times=enabled)
    if not options["journeys"]:
        return None
    bus=next(leg for leg in options["journeys"][0]["legs"] if leg["mode"]=="bus")
    assert bus["from"]["id"]==a and bus["to"]["id"]==b
    return {"line":bus["route"],"depart":bus["departure"],"arrive":bus["arrival"],
            "source":bus["calls"][0]["source"]}


def winner(witness,component):
    # A strict comparison needs two witnessed lines, and a lead larger than
    # the nominal polling interval. An earlier incompletely tracked rival
    # could have made the same through journey; count that decision as unknown.
    complete=witness["complete"]
    if not complete:
        return None,"no_complete_line"
    if len(complete)<2:
        return None,"one_complete_line"
    order=sorted((times[component],line) for line,times in complete.items())
    at,best=order[0]
    if order[1][0]-at<=45:
        return None,"close_observations"
    if any(route!=best and board<=at+45 for route,board in witness["without_exit"]):
        return None,"incomplete_early_rival"
    return best,None


def score(day, scratch, router, registry, model, signature):
    d=date.fromisoformat(day)
    folder=scratch/day
    results=[]
    for h in range(24):
        data=read(folder/f"{h:02}.gz")
        stamp=issue_stamp(d,h)
        when=datetime.fromtimestamp(stamp,IST)
        midnight=int(datetime.combine(d,time(),IST).timestamp())
        overlay=restore(registry,model,midnight,stamp,data["past"])
        assert all(p["timestamp"]<=p["received_at"]<=stamp for p in overlay.recent_passages(when))
        for (a,b,routes),witness in zip(data["cases"],data["witnesses"],strict=True):
            before=pick(router,a,b,when,overlay,False)
            after=pick(router,a,b,when,overlay,True)
            results.append({"date":day,"hour":h,"board":a,"exit":b,
                            "routes":routes,"before":before,"after":after,
                            "board_winner":winner(witness,0),"exit_winner":winner(witness,1),
                            "witnessed_lines":sorted(witness["complete"])})
        print(day,h,"scored",flush=True)
    write(folder/("scored-native-"+signature+".gz"),results)


def aggregate(rows):
    keys=("board","exit")
    summary={}
    for key in keys:
        stats=Counter()
        unknown=Counter()
        correct_lines=set()
        for row in rows:
            line,reason=row[key+"_winner"]
            old=(row["before"] or {}).get("line")
            new=(row["after"] or {}).get("line")
            stats["queries"]+=1
            if not old or not new:
                stats["without_paired_prediction"]+=1
            stats["no_before_pick"]+=int(not old)
            stats["no_after_pick"]+=int(not new)
            stats["lost_only_after"]+=int(bool(old) and not new)
            stats["gained_only_after"]+=int(bool(new) and not old)
            if row["after"] and row["after"]["source"]=="upstream_gps":
                stats["new_top_bus_anchored_by_gps"]+=1
            if row["before"] and row["after"] and row["before"]["depart"]!=row["after"]["depart"]:
                stats["first_departure_time_changed"]+=1
            if new!=old:
                stats["changed_all"]+=1
            if reason:
                unknown[reason]+=1
                continue
            if not old or not new:
                unknown["missing_prediction"]+=1
                continue
            stats["scored"]+=1
            stats["old_correct"]+=int(line==old)
            stats["new_correct"]+=int(line==new)
            seen=set(row["witnessed_lines"])
            stats["old_pick_without_complete_witness"]+=int(old not in seen)
            stats["new_pick_without_complete_witness"]+=int(new not in seen)
            if old in seen and new in seen:
                stats["both_picks_fully_witnessed"]+=1
                stats["old_correct_when_both_picks_witnessed"]+=int(line==old)
                stats["new_correct_when_both_picks_witnessed"]+=int(line==new)
            correct_lines.add(line)
            if new!=old:
                if new==line: stats["right_after_change"]+=1
                elif old==line: stats["wrong_after_change"]+=1
                else: stats["both_wrong_after_change"]+=1
        summary[key]={"counts":dict(stats),"unscored":dict(unknown),"distinct_winning_lines":len(correct_lines),
                      "scored_boarding_poles":len({r["board"] for r in rows if r[key+"_winner"][0] and r["before"] and r["after"]})}
    return summary


def runtime_version():
    return hashlib.sha256(b"".join((ROOT/"transit"/file).read_bytes() for file in
                             ("router.py","live.py","eta.py","arrivals.py","replay.py"))).hexdigest()[:16]


def report(days,scratch,router,output=None):
    rows=[]
    for day in days:
        predictions=read(scratch/day/("scored-native-"+runtime_version()+".gz"))
        truth={}
        for h in range(24):
            data=read(scratch/day/f"{h:02}.gz")
            if data.get("truth_version")!=2:
                raise ValueError("Complete the final outcome pass before publishing.")
            for (a,b,_),w in zip(data["cases"],data["witnesses"],strict=True):
                truth[h,a,b]=w
        for row in predictions:
            w=truth[row["hour"],row["board"],row["exit"]]
            row.update(board_winner=winner(w,0),exit_winner=winner(w,1),witnessed_lines=sorted(w["complete"]))
            issued=issue_stamp(date.fromisoformat(day),row["hour"])
            labels={router.routes[route][0] for route in row["routes"]}
            for run in (row["before"],row["after"]):
                if run and (run["line"] not in labels or datetime.fromisoformat(run["depart"]).timestamp()<issued):
                    raise ValueError("Returned bus does not satisfy the fixed direct journey.")
            if row["before"] and row["before"]["source"]=="upstream_gps":
                raise ValueError("Baseline contains the new position estimate.")
        if len(predictions)!=len(truth) or len({(r["hour"],r["board"],r["exit"]) for r in predictions})!=len(truth):
            raise ValueError("A dated pair is missing or repeated.")
        rows.extend(predictions)
    groups={"all":rows,
            "midnight_00_to_04":[r for r in rows if r["hour"]<5],
            "peak_08_to_10_and_17_to_19":[r for r in rows if r["hour"] in (8,9,10,17,18,19)],
            "remaining_hours":[r for r in rows if r["hour"]>=5 and r["hour"] not in (8,9,10,17,18,19)],
            "weekday":[r for r in rows if date.fromisoformat(r["date"]).weekday()<5],
            "weekend":[r for r in rows if date.fromisoformat(r["date"]).weekday()>=5]}
    candidates={rt for row in rows for rt in row["routes"]}
    witnessed_lines={ln for row in rows for ln in row["witnessed_lines"]}
    route_files=[ROOT/"transit"/p for p in ("router.py","live.py","eta.py","arrivals.py")]
    hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in [Path(__file__),*route_files]}
    fixed_data=("delhi_buses_static_gtfs_v1.zip","data/eta_model.json.gz","data/weekend/eta_model.json.gz")
    input_hashes={p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in fixed_data}
    changes=defaultdict(list)
    for r in rows:
        truth,reason=r["board_winner"]
        if reason or not r["before"] or not r["after"] or r["before"]["line"]==r["after"]["line"]:
            continue
        group="fixed" if r["after"]["line"]==truth else "lost" if r["before"]["line"]==truth else "still_wrong"
        if len(changes[group])<3:
            changes[group].append({"date":r["date"],"issue_hour":r["hour"],
                                  "board":router.stops[r["board"]]["name"],"exit":router.stops[r["exit"]]["name"],
                                  "before":r["before"],"after":r["after"],"first_witnessed_line":truth})
    result={"dates":days,"test_issue":"HH:10 IST on each calendar hour; observer already at boarding pole",
            "gps_window":"Only polls received in the previous four minutes feed both versions. Arrival scoring uses later GPS.",
            "official_route_records":len(router.routes),
            "official_distinct_line_labels":len({label for label,_ in router.routes.values()}),
            "route_count_in_candidate_pairs":len(candidates),"distinct_route_labels_in_candidate_pairs":len({router.routes[r][0] for r in candidates}),
            "distinct_fully_witnessed_route_labels":len(witnessed_lines),"distinct_pairs":len({(r["board"],r["exit"]) for r in rows}),
            "boarding_poles":len({r["board"] for r in rows}),
            "geographic_cells_about_4km":len({(int(router.stops[r["board"]]["lat"]*25),
                                              int(router.stops[r["board"]]["lon"]*22)) for r in rows}),
            "top_choice_source_before":dict(Counter((r["before"] or {}).get("source") for r in rows)),
            "top_choice_source_after":dict(Counter((r["after"] or {}).get("source") for r in rows)),
            "examples_of_changed_picks":dict(changes),
            "source_sha256":hashes,"timetable_and_models_sha256":input_hashes,
            "by_period":{g:aggregate(rs) for g,rs in groups.items()},
            "by_date":{d:aggregate([r for r in rows if r["date"]==d]) for d in days},
            "by_hour":{h:aggregate([r for r in rows if r["hour"]==h]) for h in range(24)}}
    if output:
        output.write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8")
    else:
        print(json.dumps(result,indent=2))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("stage",choices=["prepare","score","report"])
    p.add_argument("--databases",type=Path,nargs="+")
    p.add_argument("--scratch",type=Path,required=True)
    p.add_argument("--output",type=Path,help="Write the aggregate report here instead of standard output.")
    p.add_argument("--days",nargs="+",choices=DATES,default=DATES)
    p.add_argument("--pairs-per-hour",type=int,default=PAIRS_PER_HOUR,
                   help="Pair cap across geographic cells; use 0 for all scheduled competing pairs. Use a fresh scratch directory for a new panel.")
    args=p.parse_args()
    if args.stage=="prepare":
        r=Router(ROOT/"delhi_buses_static_gtfs_v1.zip",ROOT/"data",None,None)
        args.scratch.mkdir(parents=True,exist_ok=True)
        signature=args.scratch/"panel-size.txt"
        if signature.exists() and int(signature.read_text())!=args.pairs_per_hour:
            raise ValueError("Use a new scratch directory for a different panel size.")
        signature.write_text(str(args.pairs_per_hour))
        for day in args.days:
            key="bus_movements_"+day.replace("-","_")+".db"
            source=next((base/key for base in args.databases if (base/key).exists()),None)
            if source is None:
                raise FileNotFoundError(key)
            prepare(day,source,args.scratch,r,args.pairs_per_hour)
    elif args.stage=="score":
        model=EtaModel.load(ROOT/"data")
        router=Router(ROOT/"delhi_buses_static_gtfs_v1.zip",ROOT/"data",None,model)
        router._nearby=lru_cache(maxsize=50000)(router._nearby)
        registry=CapturedOverlay(model,ROOT/"data",0).bind(router)
        signature=runtime_version()
        for d in args.days:
            assert all(m.last_train_day<date.fromisoformat(d) for m in (model,model.weekend))
            score(d,args.scratch,router,registry,model,signature)
    else:
        router=Router(ROOT/"delhi_buses_static_gtfs_v1.zip",ROOT/"data",None,None)
        report(args.days,args.scratch,router,args.output)


if __name__=="__main__":
    main()
