"""Follow a fixed rider itinerary using observed bus passages and modelled walks.

This simulator does not measure a passenger or infer open doors. It follows the
first witnessed matching service even if the downstream GPS observation is lost.
"""
import argparse
from collections import Counter, defaultdict
from datetime import datetime
import gzip
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.train_eta import boundary, open_db
from tools.month_boarding_scan import PREVIOUS_EXTRACTS, archive_members, daily_database
from transit.boarding import PlatformPath
from transit.eta import IST
from transit.live import protobuf
from transit.router import Router, approximate_m, coords
from transit.walking import Walking

RADIUS = 45
MAX_GAP = 120
MAX_SPEED = 30
MIN_SEPARATION = 12


def epoch(value):
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError("Use an offset on all timestamps")
    return int(dt.timestamp())


def stamp(t):
    return datetime.fromtimestamp(t, IST).isoformat(timespec="seconds")


def fingerprint(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def clock_time(value):
    return datetime.fromtimestamp(value, IST).strftime("%H:%M:%S")


def token(run):
    return hashlib.sha256(json.dumps(run).encode()).hexdigest()[:16]


def archived_positions(path, route_ids, start, end):
    if stamp(start)[:10] != stamp(end)[:10]:
        raise ValueError("A daily source must contain the entire GPS observation window")
    with open_db(path) as db:
        a, b = boundary(db, clock_time(start)), boundary(db, clock_time(end))
        ids = sorted(int(r) for r in route_ids)
        q = ("SELECT vehicle_id,route_id,trip_id,timestamp,time,lat,lng FROM vehicle_feed "
             "WHERE rowid >= ? AND rowid < ? AND route_id IN (" + ",".join("?" for _ in ids) + ")")
        midnight = epoch(stamp(start)[:10] + "T00:00:00+05:30")
        for vehicle, route, trip, observed, received, lat, lon in db.execute(q, [a, b, *ids]):
            try:
                hour, minute, sec = (int(x) for x in received.split(":"))
                receipt = midnight + hour * 3600 + minute * 60 + sec
                yield (str(int(route)), vehicle, trip, ""), int(observed), receipt, float(lat), float(lon)
            except (TypeError, ValueError):
                continue


def live_positions(files, route_ids):
    for path in files:
        receipt = int(path.name.split("-")[1])
        feed = protobuf().FeedMessage.FromString(gzip.decompress(path.read_bytes()))
        if not 0 <= receipt - feed.header.timestamp <= MAX_GAP:
            continue
        for entity in feed.entity:
            v = entity.vehicle
            if entity.is_deleted or not entity.HasField("vehicle") or not v.HasField("position"):
                continue
            t = v.trip
            if t.route_id in route_ids and t.schedule_relationship == 0:
                yield (t.route_id, v.vehicle.id, t.trip_id, t.start_date), int(v.timestamp), receipt, v.position.latitude, v.position.longitude


def collect(rows):
    tracks = defaultdict(dict)
    counts = Counter()
    for run, observed, receipt, lat, lon in rows:
        counts["reports"] += 1
        if (not run[1] or not run[2] or not 0 <= receipt - observed <= MAX_GAP
                or not (28 < lat < 30 and 76 < lon < 79)):
            counts["rejected"] += 1
            continue
        existing = tracks[run].get(observed)
        if existing is not None:
            if existing[1:] != (lat, lon):
                tracks[run][observed] = (receipt, None, None)
                counts["conflicting_rebroadcasts"] += 1
            else:
                counts["duplicates"] += 1
            continue
        tracks[run][observed] = (receipt, lat, lon)
    counts["distinct_provider_vehicle_runs"] = len(tracks)
    return tracks, dict(counts)


def all_active(router, routes, day):
    active = router._active(day)
    return {r: [p for p in router.patterns if p.route == r and p.service in active] for r in routes}


def shape_offset(points, target):
    """Order a Google map marker on the actual published road line."""
    lat, lon = target
    east = 111195 * math.cos(math.radians(lat))
    before = 0.
    near = []
    for first, last in zip(points, points[1:]):
        x, y = (first[1]-lon)*east, (first[0]-lat)*111195
        dx, dy = (last[1]-first[1])*east, (last[0]-first[0])*111195
        length = math.hypot(dx, dy)
        if length:
            f = max(0., min(1., -(x*dx+y*dy)/length**2))
            distance = math.hypot(x+f*dx,y+f*dy)
            if distance <= RADIUS:
                near.append((distance,before+f*length))
            before += length
    if not near:
        return None
    dist, offset = min(near)
    if any(d <= dist + MIN_SEPARATION and abs(pos-offset)>90 for d,pos in near):
        return None
    return offset


def crossing_reference(points, target):
    """A short section of a directed GTFS road shape around a physical marker."""
    anchor = shape_offset(points, target)
    if anchor is None:
        return None
    lat, lon = target
    east = 111195 * math.cos(math.radians(lat))
    walked, segments = 0., []
    for first, last in zip(points, points[1:]):
        x, y = (first[1]-lon)*east, (first[0]-lat)*111195
        dx, dy = (last[1]-first[1])*east, (last[0]-first[0])*111195
        length = math.hypot(dx,dy)
        if length and walked + length >= anchor-650 and walked <= anchor+650:
            segments.append((x,y,dx/length,dy/length,length,walked-anchor))
        walked += length
    return PlatformPath(lat,lon,east,tuple(segments)) if segments else None


def crossed(paths, a, b):
    pairs = []
    for path in paths:
        start = path.project(a,35)
        end = path.project(b,35)
        if start is not None and end is not None:
            pairs.append((start,end))
    return bool(pairs) and all(start <= -RADIUS and end >= RADIUS and end-start <= 600 for start,end in pairs)


def eligible_path(patterns, run, origin, destination, offsets):
    # The exact trip is rare in these two sources. Use a provider clock only
    # when it resolves to an active pattern, then test every remaining branch.
    route, _, raw, _ = run
    exact = [p for p in patterns if raw in p.trips]
    match = re.fullmatch(re.escape(route) + r"_([0-9]{1,2})_([0-9]{2})(?:_[0-9]{1,4})?", raw)
    resolved = []
    if match:
        hour, minute = map(int, match.groups())
        if hour < 24 and minute < 60:
            time = hour * 3600 + minute * 60
            resolved = [p for p in patterns if any(start // 60 == time // 60 for start in p.starts)]
    candidates = exact or resolved or patterns
    if origin.startswith("google:") or destination.startswith("google:"):
        possible = set()
        for p in candidates:
            a = offsets.get((id(p), origin))
            b = offsets.get((id(p), destination))
            possible.add(a is not None and b is not None and b-a>=100)
        return "serves" if possible=={True} else "ambiguous" if True in possible else "cannot_board"
    boarding = [p for p in candidates if origin in p.stops]
    if not boarding:
        return "cannot_board"
    outcomes = set()
    for p in boarding:
        positions = [i for i, s in enumerate(p.stops) if s == origin]
        ends = [i for i, s in enumerate(p.stops) if s == destination]
        if len(positions) != 1:
            return "ambiguous"
        i = positions[0]
        if p.pickups[i] in (1, 2, 3):
            outcomes.add(False)
        else:
            outcomes.add(len(ends) == 1 and ends[0] > i and p.drops[ends[0]] == 0)
    return "serves" if outcomes == {True} else "cannot_board" if outcomes == {False} else "ambiguous"


def passage_candidates(router, legs, tracks, day, landmarks, use_crossings):
    routes = {rid for leg in legs for rid in leg["route_ids"]}
    service = all_active(router, routes, day)
    sid_coords = {s: coords(v) for s, v in router.stops.items()}
    sid_coords.update(landmarks)
    wanted = defaultdict(set)
    for leg in legs:
        for rid in leg["route_ids"]:
            wanted[rid].update([leg["board"], leg["alight"]])
    rivals, offsets, road_shapes = {}, {}, defaultdict(list)
    pattern_ids = {id(p):pid for pid,p in enumerate(router.patterns)}
    for rid in routes:
        sites = {s for p in service[rid] for s in p.stops}
        for sid in wanted[rid]:
            if sid in landmarks:
                # Marker and nearby GTFS stop can denote the very same landing.
                # The marker is never silently replaced by the nearest GTFS pole.
                rivals[rid, sid] = []
            else:
                rivals[rid, sid] = [sid_coords[s] for s in sites if s != sid and approximate_m(sid_coords[sid], sid_coords[s]) <= 105]
            if landmarks or use_crossings:
                shapes = {}
                for p in service[rid]:
                    if p.shape not in shapes:
                        points,_,kind = router._geometry(pattern_ids[id(p)])
                        position = shape_offset(points,sid_coords[sid]) if kind == "gtfs_shape" else None
                        shapes[p.shape] = position
                        if use_crossings and position is not None:
                            path = crossing_reference(points,sid_coords[sid])
                            if path:
                                road_shapes[rid,sid].append(path)
                    offsets[id(p),sid] = shapes[p.shape]
    result = [[] for _ in legs]
    diagnostics = Counter()
    for run, timeline in tracks.items():
        rid, _, _, service_date = run
        if service_date and service_date != day.strftime("%Y%m%d"):
            diagnostics["wrong_service_date"] += 1
            continue
        nearby = wanted[rid]
        previous = None
        block = 0
        hits = defaultdict(list)
        for t, (received, lat, lon) in sorted(timeline.items()):
            if lat is None:
                previous = None
                block += 1
                continue
            point = (lat, lon)
            if previous:
                old_t, old_point = previous
                dt = t - old_t
                if dt > MAX_GAP or approximate_m(old_point, point) > MAX_SPEED * dt:
                    block += 1
                else:
                    for sid in nearby:
                        target = sid_coords[sid]
                        distance = approximate_m(target, point)
                        old_distance = approximate_m(target, old_point)
                        if old_distance <= RADIUS:
                            continue
                        if distance <= RADIUS:
                            if any(approximate_m(other, point) - distance < MIN_SEPARATION for other in rivals[rid, sid]):
                                diagnostics["ambiguous_platform_position"] += 1
                                continue
                            kind = "outside_to_inside_gps"
                        elif use_crossings and max(distance,old_distance)<=650 and crossed(road_shapes[rid,sid],old_point,point):
                            kind = "two_outside_fixes_bracket_marker_on_directed_shape"
                        else:
                            continue
                        hits[sid].append({"lower": old_t, "upper": t, "received": received, "block": block,"evidence":kind})
            previous = t, point
        for i, leg in enumerate(legs):
            if rid not in leg["route_ids"] or not hits[leg["board"]]:
                continue
            status = eligible_path(service[rid], run, leg["board"], leg["alight"], offsets)
            if status == "cannot_board":
                continue
            boards = hits[leg["board"]]
            for board in boards:
                alight = next((h for h in hits[leg["alight"]] if h["lower"] >= board["upper"] and h["block"] == board["block"]), None)
                if len(boards) > 1 and alight and any(board["upper"] < again["lower"] < alight["upper"] for again in boards):
                    alight = None
                result[i].append({"run_token": token(run), "route_id": rid, "route": router.routes[rid][0],
                                  "pattern_status": status, "board": board, "alight": alight})
    aligned = []
    for leg in legs:
        if leg["board"] in landmarks or leg["alight"] in landmarks:
            supported = [rid for rid in leg["route_ids"] if any(
                offsets.get((id(p),leg["board"])) is not None and offsets.get((id(p),leg["alight"])) is not None
                and offsets[id(p),leg["alight"]] - offsets[id(p),leg["board"]] >= 100 for p in service[rid])]
        else:
            supported = list(leg["route_ids"])
        aligned.append(supported)
    return [sorted(cases, key=lambda c: (c["board"]["upper"], c["run_token"])) for cases in result], dict(diagnostics), aligned


def walking_distances(router, walker, scenario):
    markers = scenario.get("landmarks", {})
    def location(sid):
        return tuple(markers[sid]) if sid in markers else coords(router.stops[sid])
    places = [tuple(scenario["origin"])] + [location(leg[s]) for leg in scenario["legs"] for s in ("board", "alight")] + [tuple(scenario["destination"])]
    distances = []
    flags = []
    for i in range(0, len(places), 2):
        a, b = places[i:i+2]
        if a == b:
            distances.append(0)
            flags.append(None)
            continue
        path = walker.route(a, b)
        distances.append(None if path is None else round(path["distance_m"]))
        direct = approximate_m(a,b)
        if path is None:
            flags.append("pedestrian_graph_disconnected")
        elif path["distance_m"] > max(5*direct, direct+1000):
            flags.append("pedestrian_graph_circuitous")
        else:
            flags.append(None)
    return distances, flags


def ride_case(scenario, candidates, distances, pace, transfer_buffer, aligned, flags=None):
    departure = epoch(scenario["departure"])
    def event(kind, t, source, **kwargs):
        return {"kind": kind, "at": stamp(t), "source": source, **kwargs}
    def bracket(kind, hit, witness, stop):
        aliases = {}
        if stop.startswith("google:"):
            aliases = {"stop_label":stop.removeprefix("google:"),"route_label":leg["display_route"]}
        return event(kind, round((hit["lower"] + hit["upper"]) / 2), "bus_gps",
                     lower=stamp(hit["lower"]), upper=stamp(hit["upper"]), received=stamp(hit["received"]),
                     stop_id=stop, route=witness["route"], run_token=witness["run_token"], evidence=hit["evidence"], **aliases)
    events = [event("depart_origin", departure, "scenario_clock")]
    latest = earliest = departure
    result = {"walking_pace_mps": pace, "transfer_allowance_seconds": transfer_buffer, "events": events}
    for i, (leg, seen) in enumerate(zip(scenario["legs"], candidates)):
        if flags and flags[i]=="pedestrian_graph_circuitous":
            return {**result,"leg_reached":i+1,"status":"independent_walk_grossly_circuitous"}
        if distances[i] is None:
            return {**result, "leg_reached":i+1, "status":"no_independently_mapped_walk_to_platform"}
        seconds = round(distances[i] / pace)
        latest, earliest = latest + seconds, earliest + seconds
        when = round((latest + earliest) / 2)
        events.append(event("ready_at_stop", when, "walk_model", lower=stamp(earliest), upper=stamp(latest),
                            stop_id=leg["board"], walk_metres=distances[i], walking_seconds=seconds))
        margin = transfer_buffer if i else 0
        selectable = [c for c in seen if c["board"]["upper"] >= earliest + margin]
        result["leg_reached"] = i+1
        if not aligned[i]:
            return {**result, "status": "unresolved_route_to_map_landmarks"}
        if not selectable:
            return {**result, "status": "no_later_witnessed_boarding"}
        first = selectable[0]
        b = first["board"]
        if b["lower"] < latest + margin:
            return {**result, "status":"boarding_interval_overlaps_readiness", "boundary_witness":bracket("board", b, first, leg["board"])}
        if first["pattern_status"] == "ambiguous":
            return {**result, "status": "first_approaching_run_has_ambiguous_branch"}
        if any(c["run_token"] != first["run_token"] and c["board"]["lower"] < b["upper"] for c in selectable[1:]):
            return {**result, "status": "ordering_of_first_two_buses_unresolved"}
        events.append(bracket("board", b, first, leg["board"]))
        h = first["alight"]
        if h is None:
            return {**result, "status": "same_bus_alighting_not_witnessed"}
        events.append(bracket("alight", h, first, leg["alight"]))
        earliest, latest = h["lower"], h["upper"]
    if flags and flags[-1]=="pedestrian_graph_circuitous":
        return {**result,"status":"independent_final_walk_grossly_circuitous"}
    if distances[-1] is None:
        return {**result,"status":"no_independently_mapped_final_walk"}
    last = round(distances[-1] / pace)
    earliest, latest = earliest + last, latest + last
    events.append(event("arrive_destination", round((earliest+latest)/2), "walk_model", lower=stamp(earliest), upper=stamp(latest),
                        walk_metres=distances[-1], walking_seconds=last))
    walking = sum(e.get("walking_seconds", 0) for e in events)
    waits = sum(epoch(b["at"])-epoch(a["at"]) for a,b in zip(events,events[1:]) if b["kind"]=="board")
    bus = sum(epoch(b["at"])-epoch(a["at"]) for a,b in zip(events,events[1:]) if b["kind"]=="alight")
    total = epoch(events[-1]["at"]) - departure
    assert walking + waits + bus == total
    return {**result, "status":"complete_simulation", "walking_seconds":walking, "waiting_seconds":waits, "riding_seconds":bus,
            "door_to_door_seconds_proxy":total, "arrival_interval_ist":[stamp(earliest),stamp(latest)]}


def run(scenario, source, horizon_minutes, archive_member=None):
    walker = Walking(ROOT / "data")
    router = Router(ROOT / "delhi_buses_static_gtfs_v1.zip", ROOT / "data", walker, None)
    legs = scenario["legs"]
    routes = {rid for leg in legs for rid in leg["route_ids"]}
    departure = epoch(scenario["departure"])
    day = datetime.fromtimestamp(departure, IST).date()
    a, b = departure - 30*60, departure + horizon_minutes*60
    if source.is_dir():
        files = sorted(p for p in source.glob("positions-*.pb.gz") if a <= int(p.name.split("-")[1]) <= b)
        if not files:
            raise ValueError("No vehicle-position receipts in study window yet")
        tracks, counts = collect(live_positions(files, routes))
        hashes = {p.name: fingerprint(p) for p in files}
        received = [int(p.name.split("-")[1]) for p in files]
        fresh = [t for t,p in zip(received,files) if 0<=t-int(p.name.split("-")[2].split(".")[0])<=MAX_GAP]
        provenance = {"type":"captured_vehicle_positions", "files":hashes, "first_receipt":stamp(int(files[0].name.split("-")[1])),
                      "last_receipt":stamp(int(files[-1].name.split("-")[1])),
                      "receipt_window_ist":[stamp(a),stamp(b)], "captured_snapshots":len(files),"fresh_headers":len(fresh),
                      "maximum_fresh_snapshot_gap_seconds":max((later-earlier for earlier,later in zip(fresh,fresh[1:])),default=None),
                      "snapshot_gaps_over_120s":[[stamp(i),stamp(j)] for i,j in zip(fresh,fresh[1:]) if j-i>MAX_GAP],
                      "last_fresh_receipt_before_or_at_departure":next((stamp(t) for t in reversed(fresh) if t<=departure),None),
                      "first_fresh_receipt_after_departure":next((stamp(t) for t in fresh if t>departure),None)}
    else:
        tracks, counts = collect(archived_positions(source, routes, a, b))
        provenance = {"type":"official_october_daily_sqlite", "member":archive_member["member"] if archive_member else source.name,
                      "member_crc32":archive_member["crc32"] if archive_member else None, "first_receipt_window":stamp(a), "end_receipt_window":stamp(b),
                      "archive_sha256":"703bd6ea5237883e1ad8f094a661d365edc74b7555cf83ce4c554408a79c16c9"}
    distances, walk_flags = walking_distances(router,walker,scenario)
    def score(use_crossings):
        candidates, diag, aligned = passage_candidates(router,legs,tracks,day,scenario.get("landmarks",{}),use_crossings)
        settings = ((1.25,0),(1.0,0),(1.5,0),(1.25,90))
        cases = [ride_case(scenario,candidates,distances,pace,buf,aligned,walk_flags) for pace,buf in settings]
        # These buses were observed at the named stops. Per-leg events remain
        # reviewable even when an unverified walk or earlier bus censors the trip.
        observed = []
        for choices in candidates:
            observed.append([{**row, **{tag: {key: stamp(value) if key in ("lower","upper","received") else value for key,value in hit.items()}
                       if (hit:=row[tag]) else None for tag in ("board","alight")}} for row in choices])
        out = {"screen":diag,"route_ids_with_compatible_geometry_by_leg":aligned,"observed_boarding_entries_per_leg":list(map(len,candidates)),
               "gps_passages_by_leg":observed,"simulations":cases}
        reported = scenario.get("published_walk_metres")
        if reported is not None and any(walk_flags):
            # Keep every independently mapped, plausible walk. Substitute only
            # the flagged walk with a disclosed distance, never with an ETA.
            fallback = [published if flag else measured for flag,published,measured in zip(walk_flags,reported,distances,strict=True)]
            out["with_published_distances_for_unmapped_walks"] = {
                "distance_metres_used":fallback,
                "simulations":[ride_case(scenario,candidates,fallback,pace,buf,aligned) for pace,buf in settings]}
        return out
    return {"scenario":scenario,"source":provenance,"gps_read":counts,"walk_distance_m_access_transfers_egress":distances,
            "walk_graph_flags":walk_flags,
            **score(False),"supplemental_directed_shape_crossings":score(True)}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("scenario", type=Path)
    p.add_argument("source", type=Path, help="Position protobuf folder or dated October SQLite member")
    p.add_argument("output", type=Path)
    p.add_argument("--horizon-minutes", type=int, default=120)
    p.add_argument("--historical-date", help="Evaluate only the fixed route and clock on a different date. Discard the other date's ETA.")
    p.add_argument("--seven",type=Path,default=Path("C:/Program Files/7-Zip/7z.exe"))
    args = p.parse_args()
    scenario = json.loads(args.scenario.read_text(encoding="utf-8"))
    if args.historical_date:
        original = datetime.fromisoformat(scenario["departure"])
        changed = datetime.fromisoformat(args.historical_date + "T" + scenario["departure"].split("T")[1])
        if changed.weekday() != original.weekday():
            raise ValueError("The historical analogue must use the same weekday")
        scenario = {key:value for key,value in scenario.items() if not key.startswith("forecast")}
        scenario["original_departure"] = scenario["departure"]
        scenario["departure"] = changed.isoformat()
        scenario["test_type"] = "historical_route_analogue_without_a_prospective_forecast"
    if args.source.suffix.lower() == ".7z":
        members = archive_members(args.seven,args.source)
        member = members[scenario["departure"][:10]]
        with daily_database(args.seven,args.source,member,PREVIOUS_EXTRACTS,Path(tempfile.gettempdir())) as (db,_):
            obj = run(scenario,db,args.horizon_minutes,member)
    else:
        obj = run(scenario,args.source,args.horizon_minutes)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    pending = args.output.with_suffix(".pending")
    pending.write_text(json.dumps(obj,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
    pending.replace(args.output)
    print(args.output, obj["observed_boarding_entries_per_leg"], [(r["status"],r.get("door_to_door_seconds_proxy")) for r in obj["simulations"]])


if __name__ == "__main__":
    main()
