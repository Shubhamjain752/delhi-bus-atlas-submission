"""Read the registered predictions and the corresponding dated GPS simulations."""
import json
import hashlib
from datetime import datetime
from pathlib import Path


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def near_departure(path, key, registered):
    ledger = read(path)
    if ledger.get("kind") != "separate_near_departure_forecast" or ledger.get("case") != key or ledger.get("query") != registered:
        return {"state":"mismatched_query", "providers":{}}
    providers = {}
    departure = datetime.fromisoformat(registered["departure"])
    for name in ("atlas", "google"):
        row = ledger.get("providers", {}).get(name)
        if not row:
            continue
        state = row.get("state")
        if state not in ("captured", "no_bus_estimate"):
            providers[name] = {"state":state}
            continue
        try:
            lead = (departure - datetime.fromisoformat(row["captured_at"])).total_seconds()
        except (KeyError, TypeError, ValueError):
            lead = -1
        if not 0 < lead <= 720:
            providers[name] = {"state":"outside_capture_window"}
            continue
        providers[name] = {k: row.get(k) for k in ("state", "captured_at", "arrival", "duration_seconds", "routes", "arrival_change_seconds")}
    return {"state":ledger.get("state"), "providers":providers}


def diagnostics(result):
    crossing = result.get("supplemental_directed_shape_crossings", {})
    methods = {
        "shape_crossings": crossing,
        "published_walk_distances": result.get("with_published_distances_for_unmapped_walks", {}),
        "shape_crossings_and_published_walks": crossing.get("with_published_distances_for_unmapped_walks", {})
    }
    return {key:[{k:s.get(k) for k in ("walking_pace_mps", "transfer_allowance_seconds", "status", "leg_reached", "arrival_interval_ist",
                                       "walking_seconds", "waiting_seconds", "riding_seconds")}
                 for s in method["simulations"]] for key,method in methods.items() if method.get("simulations")}


CONTROL_METHODS = {
    "strict_radius": ("radius_entry", "strict_branch"),
    "strict_crossings": ("directed_shape_crossings", "strict_branch"),
    "confirmed_radius": ("radius_entry", "first_vehicle_confirmed_downstream"),
    "confirmed_crossings": ("directed_shape_crossings", "first_vehicle_confirmed_downstream"),
}


def checked_json(path, digest):
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("Registered control evidence has a different digest")
    return json.loads(data)


def supplemental_control(directory):
    case = "punjabi-bagh"
    study = directory / "whole-journey-control" / case
    walk = directory / "whole-journey-walk-check"
    if not (walk / "control-registration.json").is_file():
        return None
    receipt = read(walk / "control-registration-receipt.json")
    digest = receipt["sha256"]
    registration = checked_json(walk / "control-registration.json", digest)
    departure = datetime.fromisoformat(registration["departure"])
    if (registration["case"] != case or not datetime.fromisoformat(registration["registered"])
            <= datetime.fromisoformat(receipt["written_and_reread"]) < departure):
        raise ValueError("Invalid walking registration clock or case")
    def locked(path, hashes):
        return checked_json(path, hashes["data/" + path.relative_to(directory).as_posix()])
    inputs = registration["inputs_sha256"]
    protocol = locked(study / "protocol.json", inputs)
    forecasts = protocol["protocol_sha256"]
    plan = locked(study.parent / "plan.json", forecasts)["cases"][case]
    if plan["departure"] != registration["departure"]:
        raise ValueError("Control departure differs from the registered walk")
    arms = {}
    for key, route, role, forecast_file in (
        ("atlas", "159UP", "first_choice", "atlas.json"),
        ("google", "938A", "first_choice", "google.png"),
        ("google-159", "159", "matched_alternative", "google-159.png"),
    ):
        scenario = locked(study / "simulation" / f"{key}-scenario.json", inputs)
        arm = registration["arms"][key]
        raw = study / "capture" / forecast_file
        raw_digest = hashlib.sha256(raw.read_bytes()).hexdigest()
        if (raw_digest != scenario["prediction_sha256"] or raw_digest != forecasts["data/" + raw.relative_to(directory).as_posix()]
                or scenario["arm"] != key or scenario["case"] != case or scenario["departure"] != plan["departure"]
                or scenario["forecast_arrival"] != arm["forecast_arrival"]
                or datetime.fromisoformat(scenario["forecast_capture"]) >= departure):
            raise ValueError("Control forecast cannot be linked to its registered capture")
        board, alight = ("Punjabi Bagh Terminal", "Wazirpur Depot") if key == "atlas" else ("Punjabi Bagh", "Subhash Place")
        arms[key] = {"route": route, "role": role, "captured_at": scenario["forecast_capture"],
                     "forecast_arrival": arm["forecast_arrival"], "board": board, "alight": alight,
                     "board_id": arm["board"], "alight_id": arm["alight"], "access_egress_metres": arm["access_egress_metres"],
                     "simulations": {}}
    output = {"id": case, "title": plan["title"], "departure": plan["departure"], "registered": registration["registered"],
              "state": "pending", "arms": arms, "paired_159": {}}
    for suffix, state, expected in (("result", "final", "horizon_exhausted"), ("interim", "interim", "interim")):
        path = walk / f"control-walk-{suffix}.json"
        if not path.is_file():
            continue
        replay = read(path)
        obs = replay["observation"]
        if replay["registration_sha256"] != digest or obs["state"] != expected:
            raise ValueError("Walking replay belongs to a different registration or window")
        for key in arms:
            original_path = study / "simulation" / f"{key}-{suffix}.json"
            source = replay["source_files"][key]
            if source["path"] != "data/" + original_path.relative_to(directory).as_posix():
                raise ValueError("Unexpected control observation source")
            original_bytes = original_path.read_bytes()
            original = json.loads(original_bytes)
            if hashlib.sha256(original_bytes).hexdigest() != source["sha256"]:
                if suffix == "interim" and datetime.fromisoformat(original["observation"]["receipt_cutoff_ist"]) > datetime.fromisoformat(obs["receipt_cutoff_ist"]):
                    return {**output, "state":"updating"}
                raise ValueError("Walking replay does not match its GPS source")
            if (original["source"]["type"] != "captured_vehicle_positions" or original["observation"]["state"] != expected
                    or original["observation"]["receipt_cutoff_ist"] != obs["receipt_cutoff_ist"]
                    or original["scenario"]["arm"] != key or original["scenario"]["departure"] != plan["departure"]):
                raise ValueError("Invalid travel-date control source")
        for key, arm in arms.items():
            arm["simulations"] = {name: replay["arms"][key][stream][rule] for name, (stream, rule) in CONTROL_METHODS.items()}
        return {**output, "state":state, "receipt_cutoff_ist":obs["receipt_cutoff_ist"], "horizon_end_ist":obs["horizon_end_ist"],
                "source_position_receipts": replay["source_position_receipts"],
                "paired_159": {name: replay["matched_159"][stream][rule]["same_first_run_and_both_complete"]
                               for name, (stream, rule) in CONTROL_METHODS.items()}}
    return output


def field_results(directory: Path):
    root = directory / "whole-journey"
    plan = read(root / "plan.json")
    cases = []
    for key, registered in plan["cases"].items():
        folder = root / key
        case = {**registered, "id": key, "providers": {}}
        status = folder / "witness-status.json"
        if status.exists():
            case["collection"] = read(status).get("state")
        close_capture = root / "near-departure" / key / "status.json"
        if close_capture.is_file():
            case["near_departure"] = near_departure(close_capture,key,registered)
        for name in ("atlas", "google"):
            path = folder / name / "prediction.json"
            if not path.exists():
                continue
            forecast = read(path)
            if forecast.get("query") != registered or forecast.get("case") != key or forecast.get("provider") != name:
                raise ValueError("Forecast differs from registered case or provider")
            best = forecast.get("earliest")
            arm = {"captured_at": forecast["captured"], "forecast": best, "simulations": []}
            result = folder / "simulation" / (name + "-result.json")
            final = result.is_file()
            if not final:
                result = folder / "simulation" / (name + "-interim.json")
            if result.is_file():
                payload = read(result)
                scenario = payload["scenario"]
                if (scenario.get("case") != key or scenario.get("arm") != name
                        or scenario.get("departure") != registered["departure"]
                        or scenario.get("prediction_sha256") != forecast["raw_sha256"]
                        or payload.get("source", {}).get("type") != "captured_vehicle_positions"):
                    raise ValueError("Dated GPS evidence does not match its registered forecast")
                evidence = {"simulations":payload["simulations"], "diagnostics":diagnostics(payload),
                            "passages_by_leg":[{"boarding_stop":len(rows), "also_at_alighting_stop":sum(row["alight"] is not None for row in rows)}
                                               for rows in payload.get("gps_passages_by_leg", [])],
                            "source_window":{k: payload["source"].get(k) for k in ("first_receipt","last_receipt")}}
                if final:
                    arm.update(evidence)
                else:
                    arm["interim"] = evidence
            case["providers"][name] = arm
        observations = folder / "observed-legs.json"
        if observations.is_file():
            try:
                summary = read(observations)
            except (OSError, ValueError):
                summary = None
            if (isinstance(summary, dict) and summary.get("case") == key
                    and summary.get("departure") == registered["departure"] and case.get("collection") == "scored"
                    and all(case["providers"].get(name, {}).get("simulations") for name in ("atlas", "google"))):
                case["observed_legs"] = summary
        cases.append(case)
    results = {"timezone":plan["timezone"],"cases":cases}
    try:
        control = supplemental_control(directory)
    except (OSError, KeyError, TypeError, ValueError):
        control = {"id":"punjabi-bagh", "state":"unverified", "arms":{}}
    if control is not None:
        results["supplemental_control"] = control
    return results

