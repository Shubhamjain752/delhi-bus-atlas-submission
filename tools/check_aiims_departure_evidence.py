"""Replay the Sunday AIIMS decision using only packets received by each issue clock.

The local .whole-journey-witness directory holds the original receipt-named GTFS-RT
packets. The output contains route-level summaries, with no vehicle identifiers.
"""
import argparse
from collections import Counter
from datetime import datetime
import gzip
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from transit.api import Planner
from transit.arrivals import platform_estimate
from transit.eta import IST
from transit.live import TTL, protobuf
from transit.replay import CapturedOverlay, received_files, restore_entries
from transit.route_observation import ObservationOverlay
from transit.router import approximate_m, coords
from tools import score_simulated_rides as score


BOARD, EXIT = "2839", "2363"
ROUTES = {"1882": "392UP", "1980": "479UP", "2165": "711UP"}


class ReceivedOverlay(CapturedOverlay, ObservationOverlay):
    """Use the production observation/restriction adapter with a fixed replay clock."""


def stamp(epoch):
    return datetime.fromtimestamp(epoch, IST).isoformat(timespec="seconds")


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def packet(entry):
    return protobuf().FeedMessage.FromString(gzip.decompress(entry[2].read_bytes()))


def compact(journey):
    rides = [leg for leg in journey["legs"] if leg["mode"] == "bus"]
    return {
        "routes": [leg["route"] for leg in rides],
        "boarding_sources": [leg["source"] for leg in journey["legs"] if leg["mode"] == "wait"],
        "first_bus_at": next(leg["departure"] for leg in journey["legs"] if leg["mode"] == "bus"),
        "destination_at": journey["arrival"], "wait_seconds": journey["wait_seconds"],
    }


def alternatives(journey):
    """The other physically reachable lines displayed under the first bus leg."""
    bus = next(leg for leg in journey["legs"] if leg["mode"] == "bus")
    return [{k: service[k] for k in ("route", "departure_at", "alight_at", "departure_source")}
            for service in bus.get("same_platform_services", [])]


def raw_calls(last_positions, last_predictions, cutoff, planner, overlay):
    """Compare provider calls with a fresh fix for the exact same physical run."""
    positions = packet(last_positions)
    predictions = packet(last_predictions)
    current_fixes = {}
    boarding_location = coords(planner.router.stops[BOARD])
    duplicates = Counter(entity.vehicle.vehicle.id for entity in positions.entity if entity.HasField("vehicle"))
    for e in positions.entity:
        if e.is_deleted or not e.HasField("vehicle"):
            continue
        v = e.vehicle
        key = (v.trip.route_id, v.trip.start_date, v.trip.trip_id, v.vehicle.id)
        if (v.trip.route_id not in ROUTES or not v.vehicle.id or duplicates[v.vehicle.id] != 1
                or not v.HasField("position") or not 0 <= cutoff - v.timestamp <= TTL
                or not v.timestamp <= last_positions[0] <= cutoff
                or not 0 <= cutoff - positions.header.timestamp <= TTL):
            continue
        current_fixes[key] = (v.timestamp, (v.position.latitude, v.position.longitude))
    result = {route: {"fresh_vehicle_fixes": sum(key[0] == rt for key in current_fixes),
                      "paired_stop_calls": []} for rt, route in ROUTES.items()}
    for e in predictions.entity:
        if e.is_deleted or not e.HasField("trip_update"):
            continue
        u = e.trip_update
        key = (u.trip.route_id, u.trip.start_date, u.trip.trip_id, u.vehicle.id)
        if (key not in current_fixes or u.trip.schedule_relationship != 0
                or not 0 <= cutoff - predictions.header.timestamp <= TTL
                or not 0 <= cutoff - (u.timestamp or predictions.header.timestamp) <= TTL
                or (u.timestamp or predictions.header.timestamp) > last_predictions[0]):
            continue
        # Keep the two calls in direction/order and reject skipped calls.
        times = [(s.stop_id, int(s.departure.time or s.arrival.time))
                 for s in u.stop_time_update if s.schedule_relationship == 0]
        board = [(i, t) for i, (stop, t) in enumerate(times) if stop == BOARD]
        exits = [(i, t) for i, (stop, t) in enumerate(times) if stop == EXIT]
        if len(board) != 1 or len(exits) != 1 or exits[0][0] <= board[0][0] or exits[0][1] <= board[0][1]:
            continue
        seen, xy = current_fixes[key]
        service_day = datetime.strptime(key[1], "%Y%m%d").date()
        active = score.all_active(planner.router, [key[0]], service_day)[key[0]]
        scoring_key = (key[0], key[3], key[2], key[1])
        clock = overlay.clock(u.trip)
        start_candidates = [p for p in active if clock is not None and
                            any(t // 60 == clock for t in p.starts)]
        resolved = overlay.match(u.trip, [s.stop_id for s in u.stop_time_update], service_day)
        accepted = False
        if resolved is not None:
            pattern, k, _ = resolved
            matched_calls = overlay.trips_snapshot[1]
            accepted = all(matched_calls.get((service_day, pattern.trips[k], stop), (None,))[0] == value
                           for stop, value in ((BOARD, board[0][1]), (EXIT, exits[0][1])))
        result[ROUTES[key[0]]]["paired_stop_calls"].append({
            "board_at": stamp(board[0][1]), "exit_at": stamp(exits[0][1]),
            "gps_at": stamp(seen), "gps_age_seconds": cutoff - seen, "service_date": key[1],
            "straight_line_metres_to_board": round(approximate_m(xy, boarding_location)),
            "scheduled_patterns_at_provider_start_minute": len(start_candidates),
            "timetable_pair_status": score.eligible_path(active, scoring_key, BOARD, EXIT, {}),
            "exact_stop_list_and_trip_match": resolved is not None,
            "matched_stop_times_accepted_by_planner": accepted,
        })
    for route in result.values():
        route["paired_stop_calls"].sort(key=lambda row: (row["board_at"], row["gps_at"]))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    case = ROOT / "data/whole-journey/sunday-offpeak"
    capture = json.loads((case / "capture/atlas.json").read_bytes())
    timing = json.loads((case / "capture/atlas-timing.json").read_bytes())
    depart = datetime.fromisoformat(capture["departure"])
    raw = ROOT / ".whole-journey-witness" / depart.date().isoformat()
    entries = received_files(raw)
    if not entries:
        raise FileNotFoundError(raw)
    data = {"departure": depart.isoformat(), "board": BOARD, "exit": EXIT,
            "router_sha256": digest(ROOT / "transit/router.py"), "live_adapter_sha256": digest(ROOT / "transit/live.py"),
            "original_forecast_sha256": digest(case / "capture/atlas.json"),
            "original_saved_first": compact(capture["journeys"][0]),
            "clocks": []}
    planner = Planner(ROOT, online=False)
    try:
        overlay = ReceivedOverlay(planner.model, planner.directory, 0).bind(planner.router)
        last = -1
        for when in (datetime.fromisoformat(capture["issued_asof"]), depart):
            cutoff = int(when.timestamp())
            eligible = [row for row in entries if row[0] <= cutoff]
            new = [row for row in eligible if row[0] > last]
            overlay.issue = cutoff
            restore_entries(overlay, new, cutoff)
            last = cutoff
            latest = {kind: max(row for row in eligible if row[1] == kind)
                      for kind in ("positions", "predictions")}
            walks = planner.walking.route(coords(capture["origin"]), coords(planner.router.stops[BOARD]))
            ready = int(depart.timestamp()) + walks["seconds"]
            pins = planner.router.journeys(timing["origin"], timing["destination"], depart, limit=5,
                                          asof=when, eta=overlay)
            # Compare the same official platforms at the actual pin-to-platform ready time.
            at_platform = planner.router.journeys(BOARD, EXIT, datetime.fromtimestamp(ready, IST),
                                                  limit=6, max_transfers=0, asof=when, eta=overlay)
            boards = {}
            for rt, route in ROUTES.items():
                observed = platform_estimate(planner.router, overlay, BOARD, rt, when)
                boards[route] = {k: observed[k] for k in ("arrival", "source", "observed_at")}
            data["clocks"].append({
                "issued_at": when.isoformat(), "rider_ready_at": stamp(ready),
                "files_received": dict(Counter(kind for _, kind, _ in eligible)),
                "latest_packets": {kind: {"receipt": stamp(row[0]),
                    "feed_timestamp": stamp(packet(row).header.timestamp), "sha256": digest(row[2])}
                    for kind, row in latest.items()},
                "receipt_set_sha256": hashlib.sha256("\n".join(row[2].name + " " + digest(row[2])
                    for row in eligible).encode()).hexdigest(),
                "pin_journeys": [compact(j) for j in pins["journeys"]],
                "displayed_other_lines_at_first_pair": alternatives(pins["journeys"][0]),
                "platform_to_platform": [compact(j) for j in at_platform["journeys"]],
                "platform_board_at_issue": boards,
                "raw_gps_and_provider_calls": raw_calls(latest["positions"], latest["predictions"],
                    cutoff, planner, overlay),
            })
    finally:
        planner.close()
    args.output.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
