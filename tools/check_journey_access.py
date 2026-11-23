"""Replay a fixed set of Delhi journeys against any extracted version of the planner."""
import argparse
import csv
from datetime import datetime
from hashlib import sha256
import io
import inspect
import json
from pathlib import Path
import sys
import time
import zipfile


def coordinates(pair):
    return ",".join(str(value) for value in pair)


# These platforms lie one quarter and halfway along the longest stop pattern of
# each named Delhi route. Use the same pins and clocks in both builds. These are
# route-search checks; the performance of a real passenger was not measured.
PLATFORM_PAIRS = [
    ("213UP", "378", "7693", "08:30"),
    ("340UP", "1037", "1166", "13:00"),
    ("507UP", "9773", "2841", "17:30"),
    ("534UP", "2581", "7902", "08:30"),
    ("543UP", "3293", "2714", "13:00"),
    ("578UP", "3451", "3208", "17:30"),
    ("623UP", "8833", "2552", "08:30"),
    ("721UP", "5721", "1886", "13:00"),
    ("764UP", "3809", "3695", "17:30"),
    ("85UP", "3167", "2197", "08:30"),
    ("990UP", "1899", "1236", "13:00"),
]

# Before changing either router, choose mapped OSM nodes near each platform
# pair. These use a fixed offset (165m north/99m east at the origin, 82.5m
# south/165m east at the exit) before snapping to the nearest stored node.
# Keep the resulting pins fixed even if a future graph has different nodes.
STREET_PINS = {
    "213UP": ("28.6504851,77.2399481", "28.67022,77.2631889"),
    "340UP": ("28.6293108,77.2459057", "28.6477772,77.2969842"),
    "507UP": ("28.5553973,77.1802524", "28.5678775,77.2228436"),
    "534UP": ("28.6265188,77.3063238", "28.5758722,77.2671183"),
    "543UP": ("28.6229782,77.3036042", "28.5861045,77.2618836"),
    "578UP": ("28.5645167,77.1694644", "28.5366178,77.0933465"),
    "623UP": ("28.6350729,77.2827986", "28.5778834,77.2313741"),
    "721UP": ("28.6436338,77.1786917", "28.6298205,77.140118"),
    "764UP": ("28.619187,77.0331319", "28.5831854,77.0931737"),
    "85UP": ("28.6302995,77.3101079", "28.6271437,77.2464557"),
    "990UP": ("28.6438202,77.1745437", "28.6833452,77.1494149"),
}


def cases(root, timetable):
    data = root / "data"
    trial = data / "whole-journey"
    targets = json.loads((trial / "plan.json").read_text())["cases"]
    for key, q in targets.items():
        issue = json.loads((trial / key / "capture/atlas.json").read_bytes())["issued_asof"]
        yield (key, q["title"], coordinates(q["origin"]), coordinates(q["destination"]),
               q["departure"], issue, False)
    control = data / "whole-journey-control/punjabi-bagh"
    q = json.loads((control / "simulation/atlas-scenario.json").read_bytes())
    yield ("punjabi", q["title"], coordinates(q["origin"]), coordinates(q["destination"]),
           q["departure"], q["forecast_capture"], False)
    for row in json.loads((data / "nov21-planner-replay.json").read_bytes()):
        a, b = row["origin"], row["destination"]
        yield ("night-" + a["id"], a["name"] + " → " + b["name"], a["id"], b["id"],
               row["issue"], row["issue"], True)
    with zipfile.ZipFile(timetable) as z:
        with z.open("stops.txt") as stream:
            table = {r["stop_id"]: r for r in csv.DictReader(io.TextIOWrapper(stream, encoding="utf-8-sig"))}
    for route, start, end, clock in PLATFORM_PAIRS:
        a, b = table[start], table[end]
        depart = "2026-11-17T" + clock + ":00+05:30"
        yield ("corridor-" + route, a["stop_name"] + " → " + b["stop_name"],
               coordinates((a["stop_lat"], a["stop_lon"])), coordinates((b["stop_lat"], b["stop_lon"])),
               depart, depart, False)
        pins = STREET_PINS[route]
        yield ("street-" + route, "Near " + a["stop_name"] + " → near " + b["stop_name"],
               pins[0], pins[1], depart, depart, False)


def summarize(journey):
    buses = [leg for leg in journey["legs"] if leg["mode"] == "bus"]
    assert journey["duration_seconds"] == sum(journey[k] for k in ("walk_seconds", "wait_seconds", "ride_seconds"))
    return {
        "routes": [b["route"] for b in buses],
        "board": [b["from"]["id"] for b in buses],
        "alight": [b["to"]["id"] for b in buses],
        "other_lines_same_platforms": [
            {"route": s["route"], "departure": s["departure_at"]}
            for b in buses for s in b.get("same_platform_services", [])
        ],
        **{k: journey[k] for k in ("arrival", "duration_seconds", "transfers", "access_walk_seconds",
                                   "transfer_walk_seconds", "egress_walk_seconds", "wait_seconds", "ride_seconds")},
    }


def walk_probes(data, timetable, walking):
    rows = []
    checks = json.loads((data / "whole-journey-walk-check/requests.json").read_bytes())
    pairs = [(k, v["origin_lat_lon"], v["destination_lat_lon"]) for k, v in checks.items()]
    south = json.loads((data / "whole-journey-walk-check/south-extension-study.json").read_bytes())
    pairs += [(k, *v["public_pins"]) for k, v in south["links"].items()]
    with zipfile.ZipFile(timetable) as z, z.open("stops.txt") as stream:
        stops = {r["stop_id"]: (float(r["stop_lat"]), float(r["stop_lon"]))
                 for r in csv.DictReader(io.TextIOWrapper(stream, encoding="utf-8-sig"))}
    for name, start, end, _ in PLATFORM_PAIRS:
        a, b = [tuple(map(float, s.split(","))) for s in STREET_PINS[name]]
        pairs.extend([(name + "-access", a, stops[start]), (name + "-egress", stops[end], b)])
    for label, a, b in pairs:
        path = walking.route(a, b)
        rows.append({"key": label, "origin": a, "destination": b,
                     "distance_m": path["distance_m"] if path else None,
                     "seconds": path["seconds"] if path else None})
    return rows


def comparison(before, after, data):
    summary = {"before_with_journey": 0, "after_with_journey": 0, "newly_reachable": 0,
               "newly_unreachable": 0, "before_with_direct": 0, "after_with_direct": 0,
               "earlier_first": 0, "later_first": 0, "same_first_time": 0}
    rows = []
    references = {r["origin"]["id"]: r["observed"] for r in
                  json.loads((data / "nov21-planner-replay.json").read_bytes())}
    assert [q["case"] for q in before["cases"]] == [q["case"] for q in after["cases"]]
    for old, new in zip(before["cases"], after["cases"]):
        assert all(old[k] == new[k] for k in ("origin", "destination", "depart", "issue"))
        old_j, new_j = old["journeys"], new["journeys"]
        old_d = [j["routes"][0] for j in old_j if j["transfers"] == 0]
        new_d = [j["routes"][0] for j in new_j if j["transfers"] == 0]
        row = {"case": new["case"], "corridor": new["corridor"], "departure": new["depart"],
               "before": old_j[0] if old_j else None, "after": new_j[0] if new_j else None,
               "direct_before": old_d, "direct_after": new_d,
               "alternatives_before": [j["routes"] for j in old_j],
               "alternatives_after": [j["routes"] for j in new_j],
               "unverified_platform_services": new["direct_without_verified_walk"]}
        summary["before_with_journey"] += bool(old_j)
        summary["after_with_journey"] += bool(new_j)
        summary["newly_reachable"] += bool(new_j and not old_j)
        summary["newly_unreachable"] += bool(old_j and not new_j)
        summary["before_with_direct"] += bool(old_d)
        summary["after_with_direct"] += bool(new_d)
        if old_j and new_j:
            difference = new_j[0]["duration_seconds"] - old_j[0]["duration_seconds"]
            summary["earlier_first" if difference < 0 else "later_first" if difference else "same_first_time"] += 1
        if new["nov21_replay"]:
            observed = references[new["origin"]]
            reference = datetime.fromisoformat(observed["destination_passage_ist"])
            estimates = {}
            for label, query in (("before", old), ("after", new)):
                # Select the witnessed line from the planner's own suggestions.
                # A conditional bus is always labelled separately from ordinary choices.
                conditional = next((v for v in query["conditional"] if v["route"] == observed["route"]), None)
                direct = next((v for v in query["journeys"] if v["routes"] == [observed["route"]]), None)
                choice = conditional or direct
                if choice:
                    estimates[label] = {"arrival": choice["arrival"], "conditional": bool(conditional),
                                        "error_seconds": int((datetime.fromisoformat(choice["arrival"]) -
                                                              reference).total_seconds())}
            row["gps_check"] = {"route": observed["route"], "first_fix_at_destination": reference.isoformat(),
                                "predictions_for_observed_line": estimates}
        rows.append(row)
    assert [w["key"] for w in before["local_walks"]] == [w["key"] for w in after["local_walks"]]
    walks = [{**a, "before_m": b["distance_m"], "after_m": a["distance_m"]}
             for b, a in zip(before["local_walks"], after["local_walks"])]
    return {"scope": "Same origin, destination, service calendar, issue clock and route limits in each pair. "
                     "The first three have pre-departure study records; the Punjabi Bagh control also came "
                     "from a saved field case. The three night cases restore feeds by receipt time. "
                     "Other cases test route availability; changes in estimated time are not accuracy measurements. "
                     "Mapped walking distances are not field measurements.",
            "submitted_prototype_sha256": "db2d9fe4f10c4adf84d019724122a7e040a2c306fc592b0adc82e3989765b443",
            "inputs_before": before["inputs"], "inputs_after": after["inputs"],
            "journey_summary": summary, "journeys": rows, "walks": walks}


def main():
    arg = argparse.ArgumentParser(description=__doc__)
    arg.add_argument("root", type=Path, help="Planner source, timetable and data directory")
    arg.add_argument("output", type=Path)
    arg.add_argument("--before", type=Path, help="Produce a paired comparison with this saved baseline")
    args = arg.parse_args()
    root, output = args.root.resolve(), args.output.resolve()
    sys.path.insert(0, str(root))
    from transit.eta import EtaModel
    from transit.replay import load_november_capture
    from transit.route_observation import ObservationOverlay
    from transit.router import Router
    from transit.walking import Walking

    data, timetable = root / "data", root / "delhi_buses_static_gtfs_v1.zip"
    model = EtaModel.load(data)
    production_options = {"use_crossing_access": True} if "use_crossing_access" in inspect.signature(Walking).parameters else {}
    walking = Walking(data, **production_options)
    offline = ObservationOverlay(model, data)
    router = Router(timetable, data, walking, offline)
    offline.bind(router)
    result = {"inputs": {p.relative_to(root).as_posix(): sha256(p.read_bytes()).hexdigest() for p in [
        root / "transit/router.py", root / "transit/walking.py", data / "walk.npz", timetable
    ] + list(data.glob("walk-crossings.json"))}, "cases": [], "local_walks": walk_probes(data, timetable, walking)}
    output.parent.mkdir(parents=True, exist_ok=True)
    for key, title, origin, destination, depart, issue, replay in cases(root, timetable):
        clock = datetime.fromisoformat(issue)
        overlay = load_november_capture(router, model, data, int(clock.timestamp()))[0] if replay else offline
        before = time.perf_counter()
        outcome = router.journeys(origin, destination, datetime.fromisoformat(depart),
                                  limit=3, max_transfers=1, asof=clock, eta=overlay)
        row = {"case": key, "corridor": title, "origin": origin, "destination": destination,
               "depart": depart, "issue": issue, "nov21_replay": replay,
               "seconds_to_compute": round(time.perf_counter() - before, 2),
               "journeys": [summarize(j) for j in outcome["journeys"]],
               "direct_without_verified_walk": [
                   {"route": a["route"], "board": a["boarding_stop"]["id"], "alight": a["alighting_stop"]["id"],
                    "departure": a["departure_at"], "alight_at": a["alight_at"]}
                   for a in outcome.get("direct_from_nearby_platforms", [])],
               "conditional": [
                   {"route": a["route"], "seconds_before_ready": a["seconds_before_ready"],
                    "arrival": a["conditional_itinerary"]["arrival"]}
                   for a in outcome.get("boarding_alerts", []) if a.get("conditional_itinerary")
               ]}
        result["cases"].append(row)
        print(key, row["seconds_to_compute"], [j["routes"] for j in row["journeys"]], flush=True)
    if args.before:
        result = comparison(json.loads(args.before.read_bytes()), result, data)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
