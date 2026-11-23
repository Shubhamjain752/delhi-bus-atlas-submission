"""Check returned direct options at their own Delhi platforms against saved bus GPS.

Run from a research checkout containing the dated position receipts. Predictions
are kept separate from outcomes; a later position never enters the route search.
"""
from datetime import datetime
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import score_simulated_rides as gps
from transit.api import Planner


def assess(planner, folder):
    original = json.loads((folder / "capture/atlas.json").read_bytes())
    registered = json.loads((folder / "simulation/atlas-result.json").read_bytes())
    scenario = registered["scenario"]
    departure = datetime.fromisoformat(scenario["departure"])
    origin, destination = (",".join(map(str, scenario[key])) for key in ("origin", "destination"))
    revised = planner.router.journeys(
        origin, destination, departure, asof=datetime.fromisoformat(original["issued_asof"]),
        limit=len(original["journeys"]), eta=planner.live)
    direct = []
    route_ids = set()
    for version, response in (("saved", original), ("revised_replay", revised)):
        for position, journey in enumerate(response["journeys"], start=1):
            rides = [leg for leg in journey["legs"] if leg["mode"] == "bus"]
            if len(rides) == 1:
                bus = rides[0]
                route_ids.add(bus["route_id"])
                direct.append((version, position, journey, bus))
    primary = next((item for item in direct if item[0] == "revised_replay"), None)
    if primary:
        _, rank, journey, selected = primary
        ready = gps.epoch(scenario["departure"]) + journey["access_walk_seconds"]
        for service in selected.get("same_platform_services", []):
            if any(version == "revised_replay" and bus["route_id"] == service["route_id"]
                   and bus["from"]["id"] == selected["from"]["id"]
                   and bus["to"]["id"] == selected["to"]["id"] for version, _, _, bus in direct):
                continue
            route_ids.add(service["route_id"])
            trip = dict(journey, arrival=gps.stamp(gps.epoch(service["alight_at"]) + journey["egress_walk_seconds"]),
                        wait_seconds=gps.epoch(service["departure_at"]) - ready)
            direct.append(("same_stops_as_first_direct", rank, trip, {**selected, **service}))

    receipts = registered["source"]["files"]
    raw = ROOT / ".whole-journey-witness" / departure.date().isoformat()
    files = [raw / name for name in sorted(receipts)]
    if not files or any(gps.fingerprint(f) != receipts[f.name] for f in files):
        raise ValueError(f"Incomplete or different GPS receipts for {folder}")
    tracks, _ = gps.collect(gps.live_positions(files, route_ids))
    rows = []
    for version, position, journey, bus in direct:
        leg = {"route_ids": [bus["route_id"]], "board": bus["from"]["id"], "alight": bus["to"]["id"]}
        candidates, _, aligned = gps.passage_candidates(
            planner.router, [leg], tracks, departure.date(), {}, use_crossings=False)
        access = journey["access_walk_seconds"]
        egress = journey["egress_walk_seconds"]
        ready = gps.epoch(scenario["departure"]) + access
        # Strict platform scorer starts when the walker is ready; the response's
        # two actual walking durations include its stair and crossing costs.
        at_stop = {"departure": gps.stamp(ready), "legs": [leg]}
        result = gps.ride_case(at_stop, candidates, [0, 0], 1.25, 0, aligned)
        row = {
            "version": version, "rank": position, "route": bus["route"], **leg,
            "ready_at_platform": gps.stamp(ready), "access_seconds": access, "egress_seconds": egress,
            "predicted_wait_seconds": journey["wait_seconds"], "predicted_pin_arrival": journey["arrival"],
            "gps_status": result["status"],
        }
        for event in result["events"]:
            if event["kind"] in ("board", "alight"):
                row["gps_" + event["kind"] + "_interval"] = [event["lower"], event["upper"]]
        if result["status"] == "complete_simulation":
            row["modelled_destination_interval"] = [
                gps.stamp(gps.epoch(t) + egress) for t in result["arrival_interval_ist"]]
            row["wait_for_first_seen_seconds"] = result["waiting_seconds"]
        rows.append(row)
    return {
        "case": scenario["title"], "departure": departure.isoformat(),
        "before_departure_capture_sha256": gps.fingerprint(folder / "capture/atlas.json"),
        "saved_gps_result_sha256": gps.fingerprint(folder / "simulation/atlas-result.json"),
        "snapshots": len(files), "options_checked": rows,
    }


if __name__ == "__main__":
    planner = Planner(ROOT, online=False)
    try:
        cases = [assess(planner, ROOT / "data" / where) for where in (
            "whole-journey/sunday-offpeak", "whole-journey-control/punjabi-bagh")]
    finally:
        planner.close()
    print(json.dumps({
        "scope": "Exploratory replay. All direct options at the original saved limit, plus the other lines offered at the first revised direct option’s exact stops. The revised times were computed later.",
        "method": "First witnessed qualifying run after the mapped walk, using the original inside-45-metre passage checks on the same dated bus. Missing or ambiguous cases stay unscored. Walking and boarding are unobserved.",
        "cases": cases,
    }, indent=2))
