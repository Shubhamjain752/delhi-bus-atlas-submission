"""Ask which historical and same-day start evidence existed for the seven fixed November searches."""
import argparse
from collections import defaultdict
from datetime import datetime
import gzip
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tools.score_simulated_rides import collect, live_positions
from transit.live import LiveOverlay
from transit.replay import received_files
from transit.router import Router, approximate_m, coords


def strict_evidence(tracks, leg, p, k, origin, router, issue, latest_receipt, unique_clock):
    route,trip=leg["route_id"],leg["trip"]
    start=datetime.fromisoformat(leg["scheduled_start"])
    day=start.strftime("%Y%m%d")
    # Fixed November requests identify this run on its active service date.
    nominal=(p.starts[k]+p.departures[0])//60
    matched=[]
    reported_starts=set()
    for run,observations in tracks.items():
        rt,vehicle,raw,d=run
        if rt!=route or d!=day:
            continue
        eligible=sorted((t,x,y) for t,(r,x,y) in observations.items()
                        if x is not None and t<=r<=issue)
        if not eligible:
            continue
        reported_starts.add(raw)
        if raw==trip or (unique_clock and LiveOverlay.provider_clock(rt,raw)==nominal):
            matched.append((vehicle,eligible))
    witnessed=False
    for _,history in matched:
        entered=None
        previous=None
        for t,x,y in history:
            if previous is not None and t-previous>90:
                entered=None
            previous=t
            if approximate_m((x,y),coords(router.stops[p.stops[origin]]))<=45:
                entered=t
            elif (entered is not None and 5<=t-entered<=900 and
                  approximate_m((x,y),coords(router.stops[p.stops[origin+1]]))<=45):
                witnessed=True
                break
    unique=len({v for v,_ in matched})==1
    return {"observed_route_run_labels_before_issue":len(reported_starts),
            "vehicles_with_exact_or_unique_clock_before_issue":len({v for v,_ in matched}),
            "movement_past_first_stop_on_named_run_witnessed":unique and witnessed,
            "last_receipt_before_issue":latest_receipt}


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--receipts",type=Path,required=True)
    ap.add_argument("--profile",type=Path,default=ROOT/"data/origin-service-history.json.gz")
    ap.add_argument("--output",type=Path,required=True)
    args=ap.parse_args()
    with gzip.open(args.profile,"rt",encoding="utf8") as stream:
        fitted=json.load(stream)
    if fitted["training_end"]!="2026-10-31":
        raise ValueError("November audit uses history through 31 October")
    original=json.loads((ROOT/"docs/november-departure-audit.json").read_bytes())
    router=Router(ROOT/"delhi_buses_static_gtfs_v1.zip",ROOT/"data",None,None)
    trip_index={trip:(p,k) for p in router.patterns for k,trip in enumerate(p.trips)}
    result={}
    for name,case in original.items():
        clock=datetime.fromisoformat(case["issue"])
        issue=int(clock.timestamp())
        folder=(ROOT/"data/nov21-samples" if clock.date().isoformat()=="2026-11-21"
                else args.receipts/clock.date().isoformat())
        entries=received_files(folder)
        if not entries:
            raise FileNotFoundError(folder)
        packets=[p for t,kind,p in entries if kind=="positions" and t<=issue]
        # The 502UP on the stop board is a separate, conditional choice and must keep that status.
        options={"first_ordinary":case["options"][0],**({"first_conditional":case["conditional"][0]} if case["conditional"] else {})}
        routes={opt["legs"][0]["route_id"] for opt in options.values()}
        tracks,_=collect(live_positions(packets,routes))
        rows={}
        for label,option in options.items():
            leg=option["legs"][0]
            p,k=trip_index[leg["trip"]]
            origin=next(i for i,x in enumerate(p.pickups) if x==0)
            i=p.stops.index(leg["board"])
            start=datetime.fromisoformat(leg["scheduled_start"])
            board=datetime.fromisoformat(leg["board_at"])
            def key(at,pos):
                typ="weekend" if at.weekday()>=5 else "weekday"
                return "|".join([p.route,p.stops[pos],p.stops[pos+1],typ,str(at.hour//2)])
            key_origin,key_board=key(start,origin),key(board,i)
            board_summary=fitted["platform_gaps"].get(key_board)
            nominal=(p.starts[k]+p.departures[0])//60
            active=router._active(start.date())
            match_count=sum((s+other.departures[0])//60==nominal for other in router.patterns
                            if other.route==p.route and other.service in active for s in other.starts)
            last=next((datetime.fromtimestamp(t,clock.tzinfo).isoformat() for t,kind,_ in reversed(entries)
                       if kind=="positions" and t<=issue),None)
            proof=strict_evidence(tracks,leg,p,k,origin,router,issue,last,match_count==1)
            rows[label]={"route":leg["route"],"scheduled_start":leg["scheduled_start"],
                         "origin":router.stops[p.stops[origin]]["name"],"boarding":router.stops[p.stops[i]]["name"],
                         "origin_history_key":key_origin,"boarding_history_key":key_board,
                         "origin_history":fitted["buckets"].get(key_origin),
                         "boarding_history":dict(zip(("gaps","days","median_seconds","p90_seconds"),board_summary)) if board_summary else None,
                         "strict_origin_gps_test":proof}
        result[name]={"requested":clock.isoformat(),"position_packets_received_by_issue":len(packets),"first_choices":rows}
    args.output.write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8")


if __name__=="__main__":
    main()
