"""Replay the November searches at issue time, then independently score their boarding evidence."""
import argparse
from collections import Counter
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.check_eta_candidate_routes import historical_profile
from tools.score_dated_rides import ride_case
from tools.score_simulated_rides import collect, epoch, live_positions, passage_candidates
from transit.endpoints import plan_with_stop_names
from transit.live import TTL
from transit.replay import CapturedOverlay, received_files, restore_entries
from transit.router import IST, Router, _Query, approximate_m, coords
from transit.walking import Walking


def local(seconds):
    return datetime.fromtimestamp(seconds, IST).isoformat()


def describe(journey, overlay, clock):
    legs = []
    now = int(clock.timestamp())
    for leg in journey["legs"]:
        if leg["mode"] != "bus":
            continue
        route, trip = leg["route_id"], leg["trip_id"]
        board = leg["from"]["id"]
        pid, k = overlay.ids[trip]
        p = overlay.router.patterns[pid]
        midnight = clock.replace(hour=0, minute=0, second=0, microsecond=0)
        nominal = p.starts[k] + p.departures[p.stops.index(board)]
        # At midnight select the service day whose nominal clock belongs to this run.
        midnight = min((midnight + timedelta(days=d) for d in (-1, 0, 1)),
                       key=lambda m: abs((m.timestamp() + nominal) - epoch(leg["departure"])))
        day = midnight.date()
        start_minute = (p.starts[k] + p.departures[0]) // 60
        fixes = []
        for rt, run, date, seen, xy, direction, clock_key in overlay.latest_position_fixes.values():
            if (rt != route or date != day.strftime("%Y%m%d") or not 0 <= now-seen <= TTL
                    or not now-TTL <= overlay.position_snapshot[0] <= overlay.position_received <= now):
                continue
            fixes.append((round(approximate_m(xy, coords(leg["from"]))), run == trip or
                          clock_key == (rt, start_minute % 1440)))
        calls = leg["calls"]
        source = calls[0]["source"]
        item = dict(route=leg["route"], route_id=route, trip=trip, board=board, exit=leg["to"]["id"],
                    board_at=leg["departure"], exit_at=leg["arrival"], boarding_source=source,
                    scheduled_start=(midnight+timedelta(seconds=p.starts[k]+p.departures[0])).isoformat(),
                    scheduled_board=(midnight+timedelta(seconds=nominal)).isoformat(),
                    fresh_route_vehicles=len(fixes), exact_id_or_start_clock=sum(m for _,m in fixes),
                    nearest_route_fix_metres=min((d for d,_ in fixes), default=None))
        item["boarding_position"] = leg.get("boarding_position")
        item["vehicle_supported"] = bool(source == "upstream_gps" and item["boarding_position"])
        if source == "live":
            identity = overlay.forecast_vehicle(day, trip, now)
            witness = overlay.vehicle_witnesses[2].get(identity[0]) if identity else None
            item["vehicle_supported"] = bool(
                leg.get("live_run_id") or witness and witness[:3] == (day.strftime("%Y%m%d"),route,identity[1])
                and now-TTL <= witness[3] <= overlay.position_received <= now
                and (witness[5] is None or overlay.router.trip_directions.get(trip) in (None,witness[5]))
                and overlay.eligible(overlay.position_snapshot[0],now))
        legs.append(item)
    return dict(arrival=journey["arrival"], access=journey["access_walk_seconds"],
                transfer=journey["transfer_walk_seconds"], egress=journey["egress_walk_seconds"], legs=legs)


def select(options):
    # Keep the same three public choices and break ties in their existing order.
    best = epoch(options[0]["arrival"])
    supported = next((i for i,j in enumerate(options) if j["legs"][0]["vehicle_supported"]
                      and epoch(j["arrival"]) <= best + 300), 0)
    fewer = min((i for i,j in enumerate(options) if epoch(j["arrival"]) <= best+60),
                key=lambda i: (len(options[i]["legs"]),i))
    return {"current":0, "tracked_first_bus_within_five_minutes":supported, "fewer_changes_within_one_minute":fewer}


def fixed_rohini_connections(router, case, model):
    """Change only the history; hold each trip, boarding platform, walk and transfer constant."""
    clock = datetime.fromisoformat(case["query"]["departure"])
    profiles = {"four_day":historical_profile(ROOT/"data"), "full_month":model}
    result = {}
    for profile, history in profiles.items():
        search = _Query(router, clock, {"eta":history, "asof":clock})
        result[profile] = {}
        for version in profiles:
            journey = case[version][0]
            ready = search.start + journey["access_walk_seconds"]
            legs = []
            for leg in journey["legs"]:
                pid = next(i for i,p in enumerate(router.patterns) if leg["trip_id"] in p.trips)
                p = router.patterns[pid]
                ride = search.timeline(pid,p.trips.index(leg["trip_id"]),clock.date(),0)
                leave = ride.departures[p.stops.index(leg["board"])]
                arrive = ride.arrivals[p.stops.index(leg["alight"])]
                legs.append(dict(route=leg["route"],trip=leg["trip_id"],ready=search.stamp(ready),
                                 board=search.stamp(leave),alight=search.stamp(arrive),
                                 seconds_to_spare=leave-ready))
                ready = arrive + journey["transfer_walk_seconds"] + search.buffer
            result[profile][version+"_itinerary"] = legs
    return result


def daytime_score(router, query_data, options, files):
    routes = {l["route_id"] for o in options for l in o["legs"]}
    tracks, _ = collect(live_positions(files, routes))
    day = datetime.fromisoformat(query_data["departure"]).date()
    for opt in options:
        legs = [dict(route_ids=[l["route_id"]], board=l["board"], alight=l["exit"]) for l in opt["legs"]]
        hits, _, aligned = passage_candidates(router, legs, tracks, day, {}, True)
        walks = [opt["access"], *([opt["transfer"]] if len(legs)==2 else []), opt["egress"]]
        scored = ride_case(dict(departure=query_data["departure"], legs=legs), hits,
                           [s*1.25 for s in walks], 1.25, 90, aligned)
        opt["outcome"] = dict(status=scored["status"], events=[
            {k:e[k] for k in ("kind","lower","upper","evidence","stop_id") if k in e}
            for e in scored["events"] if e["kind"] in ("board","alight","arrive_destination")])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipts", type=Path, required=True, help="Original dated vehicle/prediction packet folders")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, choices=range(1,7), default=3, help="Number of public journey choices to inspect")
    parser.add_argument("--case", help="Optional case name for a single departure")
    args = parser.parse_args()
    archive = json.loads((ROOT/"docs/november-model-comparison.json").read_bytes())
    data = ROOT/"data"
    model = historical_profile(data/"from-2026-11")
    router = Router(ROOT/"delhi_buses_static_gtfs_v1.zip",data,Walking(data,use_crossing_access=True),None)
    results = {}
    cases = [(name, row["query"], args.receipts / row["query"]["departure"][:10], row) for name,row in archive["daytime"].items()]
    for row in archive["night"]:
        cases.append((row["origin"]["name"], dict(origin=row["origin"]["id"],destination=row["destination"]["id"],
                      departure=row["issue"]), data/"nov21-samples", row))
    if args.case:
        cases = [c for c in cases if c[0] == args.case]
        if not cases:
            parser.error("Unknown case")
    future = {}
    for name,q,directory,row in cases:
        clock = datetime.fromisoformat(q["departure"])
        issue = int(clock.timestamp())
        entries = received_files(directory)
        if not entries:
            raise FileNotFoundError(directory)
        prefix = [e for e in entries if e[0] <= issue]
        overlay = CapturedOverlay(model,data,issue).bind(router)
        restore_entries(overlay,prefix,issue)
        places = [",".join(map(str,p)) if isinstance(p,list) else p for p in (q["origin"],q["destination"])]
        reply = plan_with_stop_names(router,*places,clock,limit=args.limit,max_transfers=1,asof=clock,eta=overlay)
        options = [describe(j,overlay,clock) for j in reply["journeys"]]
        conditional = [describe(a["conditional_itinerary"],overlay,clock) for a in reply["boarding_alerts"]
                       if a.get("conditional_itinerary")]
        out = dict(issue=clock.isoformat(), packets=dict(Counter(k for _,k,_ in prefix)),
                   first_archive_receipt=local(entries[0][0]),
                   last_receipt_by_issue=local(prefix[-1][0]) if prefix else None,
                   options=options, policies=select(options), conditional=conditional)
        if "full_month" in row and "gps" in row:
            a,b = map(epoch,row["gps"]["receipts"])
            future[name] = [p for r,k,p in entries if k=="positions" and a<=r<=b]
            out["outcome_receipt_window"] = row["gps"]["receipts"]
        results[name] = out
        print(name, out["packets"], [(o["legs"][0]["route"],o["legs"][0]["boarding_source"],o["arrival"]) for o in options],
              out["policies"], flush=True)
    # Only now open subsequent receipts to judge the choices fixed above.
    for name,q,_,row in cases:
        out = results[name]
        if name == "tuesday-transfer":
            out["fixed_connections"] = fixed_rohini_connections(router,row,model)
        if name in future:
            daytime_score(router,q,out["options"],future[name])
        else:
            out["overnight_observed"] = row["observed"]
            assert sum(out["packets"].values()) == row["full_month"]["received"]
            assert out["options"][0]["arrival"] == row["full_month"]["ordinary"]["arrival"]
            assert [o["arrival"] for o in out["conditional"]] == [o["arrival"] for o in row["full_month"]["conditional"]]
    args.output.write_text(json.dumps(results, indent=2)+"\n",encoding="utf-8")


if __name__ == "__main__":
    main()
