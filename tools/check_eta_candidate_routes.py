"""Run identical November journey queries with two historical ETA profiles."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from transit.endpoints import plan_with_stop_names
from transit.eta import EtaModel, read_data
from transit.replay import load_november_capture
from transit.router import Router
from transit.walking import Walking


CASES = (
    ("Weekday morning", "Kashmere Gate ISBT", "Nehru Place", "2026-11-17T08:30:00+05:30"),
    ("Weekend afternoon", "28.568914,77.213693", "28.566610,77.246373", "2026-11-22T15:00:00+05:30"),
    ("Evening transfer corridor", "28.71815,77.06380", "28.70700,77.17890", "2026-11-24T17:30:00+05:30"),
    ("After midnight", "2383", "2386", "2026-11-21T01:18:00+05:30"),
)


def historical_profile(folder):
    # Both versions receive the same information: only their fitted historical profiles.
    for path in (folder / "eta_model.json.gz", folder / "weekend/eta_model.json.gz"):
        if not path.is_file():
            raise FileNotFoundError(path)
    profile = EtaModel(read_data(folder / "eta_model.json.gz"))
    profile.weekend = EtaModel(read_data(folder / "weekend/eta_model.json.gz"))
    return profile


def compare(folder, data_folder, gtfs_path, archived_502=False):
    walking = Walking(data_folder, use_crossing_access=True)
    router = Router(gtfs_path, data_folder, walking, None)
    output = {"scope": "Offline sensitivity check. All inputs apart from historical bus times are identical. "
                       "Times are retrospective estimates and do not replace the registered field forecasts.",
              "cases": []}
    for title, origin, destination, clock in CASES:
        output["cases"].append(dict(title=title, origin=origin, destination=destination, depart=clock))
    for name, path in (("existing", data_folder), ("month_candidate", folder)):
        profile = historical_profile(path)
        for case in output["cases"]:
            depart = datetime.fromisoformat(case["depart"])
            result = plan_with_stop_names(router, case["origin"], case["destination"], depart,
                                          limit=3, max_transfers=1, asof=depart, eta=profile)
            options = []
            for journey in result["journeys"]:
                if journey["duration_seconds"] != sum(journey[k] for k in ("walk_seconds", "wait_seconds", "ride_seconds")):
                    raise ValueError(f"Journey components do not reconcile for {case['title']}")
                routes = [leg["route"] for leg in journey["legs"] if leg["mode"] == "bus"]
                options.append({"routes": routes, **{k: journey[k] for k in
                                ("arrival", "duration_seconds", "walk_seconds", "ride_seconds", "wait_seconds")}})
            case[name] = options
            print(case["title"], name, len(options), options[0]["arrival"] if options else "no ordinary option", flush=True)
        if archived_502:
            depart = datetime.fromisoformat(CASES[-1][-1])
            overlay, capture = load_november_capture(router, profile, data_folder, int(depart.timestamp()))
            replay = plan_with_stop_names(router, "2383", "2386", depart, limit=3,
                                          max_transfers=1, asof=depart, eta=overlay)
            possibilities = [{"conditional_arrival": alert["conditional_itinerary"]["arrival"],
                              "fallback_arrival": alert["fallback_itinerary"]["arrival"]}
                             for alert in replay["boarding_alerts"] if alert.get("conditional_itinerary")
                             and alert.get("fallback_itinerary")]
            output.setdefault("archived_502", {})[name] = {
                "packets_received_by_issue": capture["received_files"],
                "ordinary_arrivals": [row["arrival"] for row in replay["journeys"]],
                "conditional_options": possibilities,
            }
            print("Archived 502UP", name, output["archived_502"][name], flush=True)
        del profile
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", type=Path, help="Directory containing weekday and weekend historical models")
    parser.add_argument("--data", type=Path, default=ROOT / "data")
    parser.add_argument("--gtfs", type=Path, default=ROOT / "delhi_buses_static_gtfs_v1.zip")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--check-archived-502", action="store_true", help="Also test the locally archived November operator updates when available")
    args = parser.parse_args()
    result = compare(args.candidate, args.data, args.gtfs, args.check_archived_502)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
