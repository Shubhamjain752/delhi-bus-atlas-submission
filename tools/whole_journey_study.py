"""Freeze a prospective Delhi journey forecast and score rider or simulated events."""
import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import tempfile


PROJECT = Path(__file__).resolve().parents[1]
STUDY = PROJECT / "data" / "whole-journey"
PROVIDERS = ("atlas", "google")


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def freeze_capture_set(root=STUDY):
    plan = read(root / "plan.json")
    now = datetime.now(timezone.utc)
    if now >= min(as_time(case["departure"]) for case in plan["cases"].values()):
        raise ValueError("The forecast set must be locked before the earliest departure")
    paths = [root / "plan.json"]
    for case_id in plan["cases"]:
        for provider in PROVIDERS:
            paths.extend(root / case_id / provider / name for name in ("evidence.gz", "prediction.json"))
            paths.append(root / case_id / "capture" / (provider + "-timing.json"))
        paths.append(root / case_id / "capture" / "google-transcription.json")
    record = {"locked_at": now.isoformat(),
              "sha256": {p.relative_to(root).as_posix(): digest(p) for p in paths}}
    with (root / "frozen-captures.json").open("x", encoding="utf-8") as destination:
        json.dump(record, destination, indent=2)
    return record


def verify_lock(root):
    record = read(root / "frozen-captures.json")
    for relative, expected in record["sha256"].items():
        if digest(root / relative) != expected:
            raise ValueError(f"Frozen evidence changed: {relative}")
    return len(record["sha256"])


def as_time(value):
    stamp = datetime.fromisoformat(value)
    if stamp.utcoffset() is None:
        raise ValueError(f"Time needs a UTC offset: {value}")
    return stamp


def seconds(end, start):
    return round((as_time(end) - as_time(start)).total_seconds(), 1)


def same_pin(first, second):
    return len(first) == len(second) == 2 and all(
        abs(float(a) - float(b)) < 0.000001 for a, b in zip(first, second)
    )


def normalize_atlas(raw):
    def pin(side):
        return [raw[side]["lat"], raw[side]["lon"]]

    choices = []
    for journey in raw["journeys"]:
        legs = [
            {"route": leg["route"], "board": str(leg["from"]["id"]), "alight": str(leg["to"]["id"]),
             "board_name": leg["from"]["name"], "alight_name": leg["to"]["name"]}
            for leg in journey["legs"] if leg["mode"] == "bus"
        ]
        if legs:
            choices.append({"arrival": journey["arrival"], "legs": legs,
                            **{k: journey[k] for k in ("access_walk_seconds", "transfer_walk_seconds",
                                                       "egress_walk_seconds", "wait_seconds", "ride_seconds")}})
    return {"origin": pin("origin"), "destination": pin("destination"),
            "departure": raw["departure"], "choices": choices}


def classify(case, reading):
    if not all(same_pin(case[key], reading[key]) for key in ("origin", "destination")):
        raise ValueError("Prediction endpoints differ from the fixed pins")
    if as_time(reading["departure"]) != as_time(case["departure"]):
        raise ValueError("Prediction departure differs from the fixed query")
    choices = reading["choices"]
    for choice in choices:
        if seconds(choice["arrival"], case["departure"]) <= 0 or not choice["legs"]:
            raise ValueError("Expected a later arrival and at least one bus leg")
        if any(not all(str(leg.get(k) or "").strip() for k in ("route", "board", "alight"))
               for leg in choice["legs"]):
            raise ValueError("Each option needs the route and actual boarding/alighting labels")
    ordered = sorted(choices, key=lambda c: as_time(c["arrival"]))
    return {"displayed_bus_options": choices, "earliest": next(iter(ordered), None),
            "one_change": next((c for c in ordered if len(c["legs"]) == 2), None)}


def seal(case_id, provider, evidence, captured, reading=None, unavailable=None, root=STUDY):
    case = read(root / "plan.json")["cases"][case_id]
    now = datetime.now(timezone.utc)
    if as_time(captured) > now or now >= as_time(case["departure"]):
        raise ValueError("The evidence must be captured and sealed before the planned departure")
    target = root / case_id / provider
    if target.exists():
        raise ValueError(f"Already sealed: {target}")
    if provider == "google" and reading is None and not unavailable:
        raise ValueError("Google requires a transcription of the captured options or an unavailable reason")
    source = Path(evidence)
    raw = source.read_bytes()
    if not raw:
        raise ValueError("Empty forecast evidence")
    if unavailable and reading:
        raise ValueError("An unavailable response cannot have a transcribed itinerary")
    if unavailable:
        options = {"displayed_bus_options": [], "earliest": None, "one_change": None}
    else:
        normalized = normalize_atlas(json.loads(raw.decode("utf-8-sig"))) if provider == "atlas" else read(reading)
        options = classify(case, normalized)
    record = {"case": case_id, "provider": provider, "query": case,
              "captured": captured, "sealed": now.isoformat(),
              "raw_filename": source.name, "raw_sha256": hashlib.sha256(raw).hexdigest(),
              "unavailable": unavailable, **options}
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{provider}-", dir=target.parent))
    try:
        (staging / "evidence.gz").write_bytes(gzip.compress(raw, mtime=0))
        (staging / "prediction.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        staging.rename(target)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return record


def summarize_events(events):
    """Represent only the intervals consistent with the event sequence and clocks."""
    kinds = [event["kind"] for event in events]
    if len(events) < 5 or kinds[0] != "depart_origin" or kinds[-1] != "arrive_destination":
        raise ValueError("A complete trip needs the origin, every platform event and the destination")
    middle = kinds[1:-1]
    if len(middle) % 3 or middle != ["ready_at_stop", "board", "alight"] * (len(middle) // 3):
        raise ValueError("Each bus needs a distinct ready, boarding and alighting time")
    for event in events:
        as_time(event["at"])
        expected = {"depart_origin": {"rider_clock", "scenario_clock"},
                    "ready_at_stop": {"rider_clock", "walk_model"},
                    "board": {"rider_clock", "bus_gps"},
                    "alight": {"rider_clock", "bus_gps"},
                    "arrive_destination": {"rider_clock", "walk_model"}}
        if event["source"] not in expected[event["kind"]]:
            raise ValueError("Unexpected observation source for " + event["kind"])
        if event["source"] == "bus_gps":
            if not (as_time(event["lower"]) <= as_time(event["at"]) <= as_time(event["upper"])):
                raise ValueError("GPS passage must have consistent observation bounds")
            if not event.get("evidence"):
                raise ValueError("A GPS passage needs a source trace reference")
        if event["kind"] in ("ready_at_stop", "board", "alight") and not event.get("stop_id"):
            raise ValueError("Missing platform identity")
    if any(seconds(b["at"], a["at"]) < 0 for a, b in zip(events, events[1:])):
        raise ValueError("Passenger clocks must be in chronological order")
    ride_legs, intervals, board_checks = [], [], []
    for i in range(1, len(events)-1, 3):
        previous, ready, board, alight = events[i-1:i+3]
        if ready["stop_id"] != board["stop_id"] or not board.get("route") or board["route"] != alight.get("route"):
            raise ValueError("Each wait and ride must refer to the boarded route and platform")
        if board["source"] == "bus_gps":
            margin = seconds(board["lower"], ready["at"])
            if previous["source"] == "bus_gps":
                # The rider's assumed transfer starts when the first bus reaches its stop.
                margin -= seconds(previous["upper"], previous["at"])
            if seconds(board["upper"], ready["at"]) < 0:
                raise ValueError("Vehicle passage ended before the rider reached the platform")
            board_checks.append({"route": board["route"], "board": board["stop_id"],
                                 "earliest_passage_minus_readiness_seconds": margin,
                                 "order": "ready_by_lower_bound" if margin >= 0 else "overlap"})
        leg = {"route": board["route"], "board": board["stop_id"], "alight": alight["stop_id"]}
        for event, field, label in ((board, "route_label", "route_label"), (board, "stop_label", "board_label"),
                                    (alight, "stop_label", "alight_label")):
            if event.get(field):
                if not event.get("matching_evidence"):
                    raise ValueError("Cross-provider route and stop labels require a physical match reference")
                leg[label] = event[field]
        ride_legs.append(leg)
        for name, a, b in (("access_walk" if i == 1 else "transfer_walk",previous,ready),
                           ("wait",ready,board), ("ride",board,alight)):
            intervals.append({"stage": name, "seconds": seconds(b["at"],a["at"]),
                              "endpoint_sources": [a["source"], b["source"]]})
    intervals.append({"stage": "egress_walk", "seconds": seconds(events[-1]["at"],events[-2]["at"]),
                      "endpoint_sources": [events[-2]["source"],events[-1]["source"]]})
    return {"kind": "observed_passenger" if all(e["source"] == "rider_clock" for e in events) else "simulation",
            "departure": events[0]["at"], "arrival": events[-1]["at"], "legs": ride_legs, "stages": intervals,
            "simulated_boarding_checks": board_checks}


def equivalent(legs, observed):
    def labels(leg, key):
        return {str(value).strip().casefold() for value in (leg.get(key), leg.get(key+"_id"), leg.get(key+"_label"))
                if value is not None}
    return len(legs) == len(observed) and all(
        labels(a,key) & labels(b,key) for a,b in zip(legs,observed) for key in ("route","board","alight")
    )


def report(case_id, events=None, root=STUDY):
    if (root / "frozen-captures.json").is_file():
        verify_lock(root)
    case = read(root / "plan.json")["cases"][case_id]
    outcome = summarize_events(events) if events else None
    result = {"case": case_id, "query": case, "outcome": outcome, "predictions": {}}
    for provider in PROVIDERS:
        directory = root / case_id / provider
        if not directory.is_dir():
            result["predictions"][provider] = {"state": "unavailable"}
            continue
        forecast = read(directory / "prediction.json")
        raw = gzip.decompress((directory / "evidence.gz").read_bytes())
        if hashlib.sha256(raw).hexdigest() != forecast["raw_sha256"]:
            raise ValueError("Raw evidence digest differs from sealed ledger")
        if forecast["query"] != case or forecast["provider"] != provider or not (
            as_time(forecast["captured"]) <= as_time(forecast["sealed"]) < as_time(case["departure"])
        ):
            raise ValueError("Prediction ledger disagrees with the preregistered query")
        entry = {"state": "captured" if forecast["earliest"] else "no_bus_estimate",
                 "reason": forecast["unavailable"], "captured": forecast["captured"],
                 "lead_seconds": seconds(case["departure"], forecast["captured"])}
        selections = ["earliest", "one_change"] if case_id == "tuesday-transfer" else ["earliest"]
        for key in selections:
            choice = forecast[key]
            if choice is None:
                entry[key] = None
                continue
            arm = {**choice, "journey_seconds": seconds(choice["arrival"],case["departure"])}
            if outcome:
                matched = equivalent(choice["legs"],outcome["legs"])
                same_start = (int(as_time(outcome["departure"]).timestamp()) // 60 ==
                              int(as_time(case["departure"]).timestamp()) // 60)
                arm["matches_taken_legs"] = matched
                arm["actual_minus_requested_departure_seconds"] = seconds(outcome["departure"],case["departure"])
                clear_order = all(c["order"] != "overlap" for c in outcome["simulated_boarding_checks"])
                if matched and same_start and clear_order:
                    arm["predicted_minus_" + outcome["kind"] + "_arrival_seconds"] = seconds(choice["arrival"],outcome["arrival"])
            entry[key] = arm
        result["predictions"][provider] = entry
    forecasts = result["predictions"]
    if all("captured" in entry for entry in forecasts.values()):
        result["capture_time_difference_seconds"] = abs(seconds(forecasts["atlas"]["captured"],forecasts["google"]["captured"]))
    return result


def simulation_report(case_id, outcome_file, root=STUDY):
    data = read(outcome_file)
    scenario = data["scenario"]
    provider = scenario["arm"]
    if provider not in PROVIDERS or scenario["case"] != case_id:
        raise ValueError("The result is from a different trial or planner")
    if scenario != read(root / case_id / "simulation" / (provider+"-scenario.json")):
        raise ValueError("The result is outside the registered trip. Historical analogues cannot score a prospective forecast")
    forecast = read(root / case_id / provider / "prediction.json")
    if scenario["prediction_sha256"] != forecast["raw_sha256"]:
        raise ValueError("The simulation used a different forecast")
    primary = next(s for s in data["simulations"] if s["walking_pace_mps"] == 1.25 and s["transfer_allowance_seconds"] == 0)
    info = {"arm": provider, "status": primary["status"], "events": primary["events"],
            "leg_reached": primary.get("leg_reached"), "result_sha256": digest(outcome_file),
            "walking_pace_mps": 1.25, "transfer_allowance_seconds": 0}
    if primary["status"] != "complete_simulation":
        result = report(case_id,root=root)
        result["simulation"] = info
        return result
    events = [dict(e) for e in primary["events"]]
    if provider == "google":
        name = scenario.get("marker_evidence","map-marker-readings.json")
        markers = root / case_id / "capture" / name
        if digest(markers) != scenario["marker_sha256"]:
            raise ValueError("Google stop positions differ from the scenario")
        for event in events:
            if event.get("route_label") or event.get("stop_label"):
                event["matching_evidence"] = f"{case_id}/capture/{name} ({scenario['marker_sha256']})"
    result = report(case_id,events,root)
    predicted = result["predictions"][provider]["earliest"]
    if predicted.get("predicted_minus_simulation_arrival_seconds") is not None:
        early, late = primary["arrival_interval_ist"]
        predicted["predicted_minus_simulated_arrival_interval_seconds"] = [
            seconds(predicted["arrival"],late),seconds(predicted["arrival"],early)]
    result["simulation"] = info
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    freeze = sub.add_parser("seal")
    freeze.add_argument("case")
    freeze.add_argument("provider",choices=PROVIDERS)
    freeze.add_argument("--source",type=Path,required=True)
    freeze.add_argument("--captured",required=True)
    freeze.add_argument("--reading",type=Path)
    freeze.add_argument("--unavailable")
    summary = sub.add_parser("report")
    summary.add_argument("case")
    observed = summary.add_mutually_exclusive_group()
    observed.add_argument("--events",type=Path)
    observed.add_argument("--simulation",type=Path,help="Read a full scorer output including incomplete journeys")
    summary.add_argument("--output",type=Path)
    sub.add_parser("lock")
    sub.add_parser("verify")
    args = parser.parse_args()
    if args.command == "lock":
        print("Locked", len(freeze_capture_set()["sha256"]), "files")
    elif args.command == "verify":
        print("Verified", verify_lock(STUDY), "frozen evidence files")
    elif args.command == "seal":
        result = seal(args.case,args.provider,args.source,args.captured,args.reading,args.unavailable)
        print(f"Sealed {args.provider} for {args.case}: {result['raw_sha256']}")
    else:
        result = (simulation_report(args.case,args.simulation) if args.simulation else
                  report(args.case,read(args.events) if args.events else None))
        rendered = json.dumps(result,ensure_ascii=False,indent=2)
        if args.output:
            args.output.write_text(rendered+"\n",encoding="utf-8")
        else:
            print(rendered)


if __name__ == "__main__":
    main()
