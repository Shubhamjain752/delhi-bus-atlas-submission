"""Audit the first GPS-witnessed direct services on the Sunday AIIMS stop pair.

Run separately from check_aiims_departure_evidence.py. This script reads later
bus positions as outcomes. It cannot supply inputs to the departure replay.
"""
import argparse
from datetime import datetime
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from transit.api import Planner
from transit.eta import IST
from transit.replay import received_files
from transit.router import approximate_m, coords
from tools import score_simulated_rides as gps


BOARD, EXIT = "2839", "2363"


def through_pair(pattern):
    if pattern.stops.count(BOARD) != 1 or pattern.stops.count(EXIT) != 1:
        return False
    board, exit_ = pattern.stops.index(BOARD), pattern.stops.index(EXIT)
    return board < exit_ and pattern.pickups[board] == 0 and pattern.drops[exit_] == 0


def bounds(hit):
    return [gps.stamp(hit[t]) for t in ("lower", "upper")] if hit else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    case = ROOT / "data/whole-journey/sunday-offpeak"
    outcome_file = case / "simulation/atlas-result.json"
    manifest = json.loads(outcome_file.read_bytes())
    capture = json.loads((case / "capture/atlas.json").read_bytes())
    depart = datetime.fromisoformat(capture["departure"])
    departure = int(depart.timestamp())
    folder = ROOT / ".whole-journey-witness" / depart.date().isoformat()
    files = []
    for name, checksum in sorted(manifest["source"]["files"].items()):
        path = folder / name
        if gps.fingerprint(path) != checksum:
            raise ValueError(f"Different witness packet: {name}")
        files.append(path)
    planner = Planner(ROOT, online=False)
    try:
        router = planner.router
        active = router._active(depart.date())
        patterns = [(pid, p) for pid, p in enumerate(router.patterns)
                    if p.service in active and through_pair(p)]
        route_ids = {p.route for _, p in patterns}
        stops = {s: coords(router.stops[s]) for s in (BOARD, EXIT)}
        walk = planner.walking.route(coords(capture["origin"]), stops[BOARD])
        ready = departure + walk["seconds"]
        tracks, _ = gps.collect(gps.live_positions(files, route_ids))
        leg = {"board": BOARD, "alight": EXIT, "route_ids": sorted(route_ids)}
        hits, diagnostics, _ = gps.passage_candidates(router, [leg], tracks, depart.date(), {}, False)
        # Show all candidate passages near the choice, before any exclusion for a missing exit.
        observed = [{"route": h["route"], "pattern_status": h["pattern_status"],
                     "boarding_interval": bounds(h["board"]), "exit_interval": bounds(h["alight"]),
                     "entry_after_walker_ready": h["board"]["lower"] >= ready}
                    for h in hits[0] if ready <= h["board"]["upper"] <= ready + 1800]
        # Identify the 711 already visible at departure by its two distinct raw
        # identifiers. Do not pick a later vehicle that happens to use the same line.
        receipt, _, latest = max(row for row in received_files(folder)
                                 if row[1] == "positions" and row[0] <= departure)
        visible = [(key, t) for key,t,_,_,_ in gps.live_positions([latest], {"2165"})
                   if 0 <= departure - t <= gps.MAX_GAP]
        details = []
        for key, t in visible:
            near = [(seen, (a, b)) for seen, (_, a, b) in sorted(tracks.get(key, {}).items())
                    if departure <= seen <= departure + 1800 and a is not None]
            if not near:
                continue
            inside = [(i, seen, point) for i,(seen,point) in enumerate(near)
                      if approximate_m(stops[BOARD], point) <= gps.RADIUS]
            if not inside:
                continue
            first, last = inside[0][0], inside[-1][0]
            entry = near[first-1:first+1] if first else []
            crossing_points = near[max(0, first-1):last+2]
            direct_paths = [router.platform_path(pid, pattern.stops.index(BOARD))
                            for pid, pattern in patterns if pattern.route == key[0]]
            # The GPS should advance along at least one published eastbound geometry here.
            distances = []
            for path in direct_paths:
                if path is None:
                    continue
                offsets = [path.project(point) for _, point in crossing_points]
                if all(m is not None for m in offsets) and all(a <= b for a, b in zip(offsets, offsets[1:])):
                    distances.append([round(m) for m in offsets])
            active_711 = gps.all_active(router, [key[0]], depart.date())[key[0]]
            details.append({
                "route": router.routes[key[0]][0], "last_fix_available_at_departure": gps.stamp(t),
                "feed_received": gps.stamp(receipt), "service_date": key[3],
                "strict_pattern_status_for_pair": gps.eligible_path(active_711,key,BOARD,EXIT,{}),
                "first_entry_window": [gps.stamp(seen) for seen, _ in entry],
                "fixes_inside_boarding_area": [{"at":gps.stamp(seen),
                    "metres_to_platform":round(approximate_m(stops[BOARD], point))} for _,seen,point in inside],
                "signed_metres_along_published_boarding_approach": distances[0] if distances else None,
                "approach_fix_times": [gps.stamp(seen) for seen,_ in crossing_points],
                "closest_exit_fix_metres_within_half_hour": round(min(approximate_m(stops[EXIT], pt) for _,pt in near)),
            })
        result = {
            "departure": depart.isoformat(), "rider_ready_at": gps.stamp(ready), "board": BOARD, "exit": EXIT,
            "frozen_position_receipt_manifest_sha256": gps.fingerprint(outcome_file),
            "frozen_position_files": len(files), "all_active_direct_route_ids": len(route_ids),
            "route_labels": sorted({router.routes[r][0] for r in route_ids}),
            "routes_with_usable_vehicle_runs": len({key[0] for key in tracks}),
            "strict_first_half_hour_boardings_including_missing_exits": observed,
            "strict_matching_diagnostics": dict(diagnostics),
            "physical_711_seen_at_departure": details,
            "scope": "First witnessed service at the two specified stops. GPS gaps and actual boardings are unknown. Physical proximity is listed separately when a run's timetable stopping pattern disagrees.",
        }
    finally:
        planner.close()
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
