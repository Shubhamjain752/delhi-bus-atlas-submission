"""Repeat the paired November journey searches and optionally check recorded vehicle passages."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.check_eta_candidate_routes import historical_profile
from transit.endpoints import plan_with_stop_names
from transit.replay import load_november_capture
from transit.router import Router
from transit.walking import Walking


def query(router, places, clock, model):
    return plan_with_stop_names(router, *places, clock, limit=3, max_transfers=1, asof=clock, eta=model)


def verify_option(actual, recorded):
    assert actual["arrival"] == recorded["arrival"]
    assert actual["duration_seconds"] == sum(actual[k] for k in ("wait_seconds", "ride_seconds", "walk_seconds"))
    bus_legs = [leg for leg in actual["legs"] if leg["mode"] == "bus"]
    assert [(leg["trip_id"], leg["from"]["id"], leg["to"]["id"], leg["arrival"]) for leg in bus_legs] == [
        (leg["trip_id"], leg["board"], leg["alight"], leg["arrival"]) for leg in recorded["legs"]]


def verify_gps(router, case, directory):
    from tools.score_dated_rides import ride_case
    from tools.score_simulated_rides import collect, epoch, live_positions, passage_candidates

    clock = datetime.fromisoformat(case["query"]["departure"])
    routes = [*case["full_month"], case["four_day"][0]]
    wanted = {leg["route_id"] for option in routes for leg in option["legs"]}
    first, last = map(epoch, case["gps"]["receipts"])
    receipts = sorted(p for p in (directory / str(clock.date())).glob("positions-*.pb.gz")
                      if first <= int(p.name.split("-")[1]) <= last)
    if not receipts:
        raise FileNotFoundError(f"No captured GPS files for {clock.date()} in {directory}")
    tracks, _ = collect(live_positions(receipts, wanted))
    for option, recorded in zip(routes, case["gps"]["checks"], strict=True):
        legs = [{"route_ids":[leg["route_id"]], "board":leg["board"], "alight":leg["alight"]} for leg in option["legs"]]
        candidates, _, aligned = passage_candidates(router, legs, tracks, clock.date(), {}, True)
        walks = [option["access_walk_seconds"]]
        if len(legs) == 2:
            walks.append(option["transfer_walk_seconds"])
        walks.append(option["egress_walk_seconds"])
        # The scorer accepts distances. Preserve each mapped walk's duration including crossing penalties.
        simulated = ride_case({**case["query"], "legs":legs}, candidates, [s*1.25 for s in walks], 1.25, 90, aligned)
        assert simulated["status"] == recorded["chain_status"]
        assert [e["at"] for e in simulated["events"]] == [e["at"] for e in recorded["chain_events"]]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gtfs", type=Path, default=ROOT / "delhi_buses_static_gtfs_v1.zip")
    parser.add_argument("--witness-dir", type=Path, help="Dated folders of originally recorded November position packets")
    parser.add_argument("--overnight", action="store_true", help="Also replay the original operator packets in data/nov21-samples")
    args = parser.parse_args()
    evidence = json.loads((ROOT / "docs/november-model-comparison.json").read_bytes())
    data = ROOT / "data"
    router = Router(args.gtfs, data, Walking(data, use_crossing_access=True), None)
    models = {"four_day":historical_profile(data), "full_month":historical_profile(data / "from-2026-11")}
    for name, case in evidence["daytime"].items():
        request = case["query"]
        places = [",".join(map(str,request[key])) for key in ("origin","destination")]
        clock = datetime.fromisoformat(request["departure"])
        for version, model in models.items():
            actual = query(router, places, clock, model)["journeys"]
            for found, saved in zip(actual, case[version], strict=True):
                verify_option(found, saved)
            print(name, version, actual[0]["arrival"])
        if args.witness_dir:
            verify_gps(router, case, args.witness_dir)
    if args.overnight:
        for row in evidence["night"]:
            clock = datetime.fromisoformat(row["issue"])
            for version, model in models.items():
                overlay, meta = load_november_capture(router, model, data, int(clock.timestamp()))
                reply = query(router, [row["origin"]["id"], row["destination"]["id"]], clock, overlay)
                verify_option(reply["journeys"][0], row[version]["ordinary"])
                conditional = [a["conditional_itinerary"]["arrival"] for a in reply["boarding_alerts"] if a.get("conditional_itinerary")]
                assert conditional == [a["arrival"] for a in row[version]["conditional"]]
                assert meta["received_files"] == row[version]["received"]
                print(row["origin"]["name"], version, conditional or reply["journeys"][0]["arrival"])


if __name__ == "__main__":
    main()
