"""Compare the independent AIIMS approach with the saved, dated 433 bus passages."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.score_dated_rides import ride_case
from tools.score_simulated_rides import epoch


def check():
    folder = ROOT / "data/whole-journey-walk-check"
    routes = folder / "summary.json"
    capture = ROOT / "data/whole-journey/sunday-offpeak/simulation/google-first-complete.json"
    measurement = json.loads(routes.read_bytes())["aiims-access"]
    evidence = json.loads(capture.read_bytes())
    scenario = evidence["scenario"]
    if scenario["arm"] != "google" or scenario["case"] != "sunday-offpeak" or measurement["public_steps_modes"] != ["walking"]:
        raise ValueError("Expected the AIIMS pedestrian link and the recorded Sunday Google itinerary")
    candidates = evidence["gps_passages_by_leg"]
    for leg in candidates:
        for vehicle in leg:
            for end in ("board", "alight"):
                if vehicle[end]:
                    for key in ("lower", "upper", "received"):
                        vehicle[end][key] = epoch(vehicle[end][key])
    distances = [measurement["including_straight_endpoint_offsets_metres"], evidence["walk_distance_m_access_transfers_egress"][-1]]
    return {"type":"post_departure_pedestrian_sensitivity", "evaluated_at":datetime.now().astimezone().isoformat(timespec="seconds"),
            "source_sha256":{p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in (capture,routes)},
            "approach_and_egress_metres":distances, "forecast":scenario["forecast_arrival"],
            "simulations":[ride_case(scenario,candidates,distances,p,0,evidence["route_ids_with_compatible_geometry_by_leg"])
                           for p in (1.25,1.0,1.5)]}


if __name__ == "__main__":
    result = check()
    destination = ROOT / "data/whole-journey-walk-check/aiims-travel-date.json"
    existing = json.loads(destination.read_bytes()) if destination.is_file() else {}
    if {k:v for k,v in result.items() if k != "evaluated_at"} != {k:v for k,v in existing.items() if k != "evaluated_at"}:
        temp = destination.with_suffix(".tmp")
        temp.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        temp.replace(destination)
    for s in result["simulations"]:
        print(s["walking_pace_mps"], s["status"], s.get("arrival_interval_ist"), s.get("walking_seconds"), s.get("waiting_seconds"))
