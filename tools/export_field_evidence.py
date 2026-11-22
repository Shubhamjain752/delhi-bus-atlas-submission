"""Export the dated whole-journey study for reviewers without the full planner."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import tempfile
import zipfile
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.whole_journey_study import STUDY, verify_lock
from transit.field_trials import field_results

OUTPUT = ROOT / "Delhi-Bus-Atlas-Journey-Evidence.zip"


def local_clock(iso):
    return datetime.fromisoformat(iso).strftime("%H:%M:%S")


def delta_text(seconds):
    minutes, rest = divmod(round(seconds), 60)
    return f"{minutes}m {rest:02}s" if minutes else f"{rest}s"


def forecast_error(eta, interval):
    predicted = datetime.fromisoformat(eta)
    first, last = map(datetime.fromisoformat, interval)
    if first <= predicted <= last:
        return "Within the GPS-derived interval"
    bounds = sorted(abs((predicted - t).total_seconds()) for t in (first, last))
    direction = "early" if predicted < first else "late"
    return f"{delta_text(bounds[0])} to {delta_text(bounds[1])} {direction}"


def outcome(arm):
    for simulation in arm["simulations"]:
        if simulation["walking_pace_mps"] != 1.25 or simulation["transfer_allowance_seconds"] != 0:
            continue
        if simulation["status"] == "complete_simulation":
            bounds = simulation["arrival_interval_ist"]
            return " to ".join(local_clock(t) for t in bounds), forecast_error(arm["forecast"]["arrival"], bounds)
        statuses = {
            "no_later_witnessed_boarding": "No subsequent bus verified at the boarding stop",
            "same_bus_alighting_not_witnessed": "Bus seen at boarding, no verified passage at alighting",
            "independent_walk_grossly_circuitous": "Implausible mapped walk to the boarding stop",
            "unresolved_route_to_map_landmarks": "The published route does not match both mapped stops",
            "first_approaching_run_has_ambiguous_branch": "Branch of the first observed bus is uncertain",
            "boarding_interval_overlaps_readiness": "First observed bus may have passed during the walk",
        }
        reason = statuses.get(simulation["status"], simulation["status"].replace("_", " "))
        return f"Leg {simulation.get('leg_reached', 1)}. {reason}", "Unscored"
    return "No dated result available at export", "Pending"


def control_summary():
    folder = ROOT / "data/whole-journey-walk-check"
    final = folder / "control-walk-result.json"
    result_file = final if final.is_file() else folder / "control-walk-interim.json"
    if not result_file.is_file():
        return []
    result = json.loads(result_file.read_text(encoding="utf-8"))
    registration_file = folder / "control-registration.json"
    if hashlib.sha256(registration_file.read_bytes()).hexdigest() != result["registration_sha256"]:
        raise ValueError("Control replay differs from the registered walking sensitivity")
    registered = json.loads(registration_file.read_text(encoding="utf-8"))["arms"]
    observed = result["observation"]
    window = "Final observations" if observed["state"] == "horizon_exhausted" else "Interim observations"
    lines = ["", "## Punjabi Bagh to Wazirpur Depot", "",
             f"Sunday 15:10. The independent pedestrian-route sensitivity was registered before departure. Results below use its stop-radius GPS passages and original branch requirement. {window} through {local_clock(observed['receipt_cutoff_ist'])} IST. Atlas starts and finishes at its published poles. The Google itineraries include modelled walks of 10 and 28 seconds.", "",
             "| Forecast | Predicted arrival | First witnessed bus at boarding | Simulated arrival | Forecast difference |",
             "|---|---:|---|---|---|"]
    for arm, name in (("atlas", "Atlas 159UP"), ("google", "Google first choice 938A"), ("google-159", "Google same-line alternative 159")):
        eta = registered[arm]["forecast_arrival"]
        simulation = result["arms"][arm]["radius_entry"]["strict_branch"]
        entry = next((e for e in simulation["events"] if e["kind"] == "board"), None)
        board = " to ".join(local_clock(entry[k]) for k in ("lower", "upper")) if entry else "Unverified"
        end, difference = outcome({"simulations":[simulation], "forecast":{"arrival":eta}})
        lines.append(f"| {name} | {local_clock(eta)} | {board} | {end} | {difference} |")
    for arm, methods in result["arms"].items():
        seen = methods["radius_entry"]["strict_branch"]
        along = methods["directed_shape_crossings"]["strict_branch"]
        if seen["status"] != "complete_simulation" and along["status"] == "complete_simulation":
            eta = registered[arm]["forecast_arrival"]
            end, difference = outcome({"simulations":[along], "forecast":{"arrival":eta}})
            label = {"atlas":"Atlas 159UP", "google":"Google 938A", "google-159":"Google 159"}[arm]
            lines.extend(["", f"For {label}, the separate directed-road crossing check places arrival at {end}. The saved forecast is {difference} under that assumption. The destination passage falls between GPS fixes."])
    lines.extend(["", "[Control registration and GPS](docs/whole-journey-control.md) · [Pedestrian checks and time breakdown](docs/walking-crosscheck.md)"])
    return lines


def summary(data):
    lines = ["# Delhi whole-journey evidence", "",
             "Forecast times below were recorded before departure. The comparison follows the first witnessed matching bus on each planner’s route. The virtual traveller walks at 1.25 m/s with no additional transfer allowance. Bus positions give arrival intervals. A person’s boarding, walk and door-to-door time were not observed.", "",
             "| Departure (Delhi) | Journey | Planner | Predicted arrival | GPS and modelled walk | Forecast difference |",
             "|---|---|---|---:|---|---|"]
    for case in data["cases"]:
        depart = datetime.fromisoformat(case["departure"]).strftime("%d %b %H:%M")
        for key, name in (("atlas", "Atlas"), ("google", "Google Maps")):
            arm = case["providers"][key]
            forecast = arm["forecast"]
            arrival = local_clock(forecast["arrival"]) if forecast else "No bus forecast"
            end, difference = outcome(arm)
            lines.append(f"| {depart} | {case['title']} | {name} | {arrival} | {end} | {difference} |")
    aiims = ROOT / "data/whole-journey-walk-check/aiims-travel-date.json"
    if aiims.is_file():
        check = json.loads(aiims.read_bytes())
        for relative, checksum in check["source_sha256"].items():
            if hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() != checksum:
                raise ValueError("The AIIMS pedestrian check refers to different inputs")
        end, difference = outcome({"simulations":check["simulations"], "forecast":{"arrival":check["forecast"]}})
        lines.extend(["", "### AIIMS walking sensitivity", "",
                      f"Using the separately checked pedestrian route to the 433 and a 1.25 m/s pace, the simulated destination arrival is {end}. The saved Google ETA is {difference}. The bundled pedestrian path leaves the primary score open. See the two observed stops and how the assumed walk changes waiting time in the [Sunday travel-date results](docs/whole-journey-fieldwork.md#sunday-travel-date-results)."])
    lines.extend(control_summary())
    lines.extend(["", "The capture directories retain the screenshots, forecast receipts and route selection. Each dated simulation result includes its walking, waiting and riding events, the walking-speed sensitivity and the GPS evidence available. The October analogues use different travel dates.", "",
                  "[Study and forecasts](docs/whole-journey-fieldwork.md) · [How GPS is scored](docs/simulated-rides.md)"])
    return "\n".join(lines) + "\n"


def evidence_files():
    for name in ("whole-journey", "whole-journey-control", "whole-journey-walk-check"):
        for path in sorted((ROOT / "data" / name).rglob("*")):
            if path.is_file() and path.suffix in {".json", ".gz", ".txt", ".png"} and not path.name.startswith("explore-"):
                yield path
    for name in ("whole-journey-fieldwork.md", "whole-journey-control.md", "simulated-rides.md", "walking-crosscheck.md"):
        path = ROOT / "docs" / name
        if path.exists():
            yield path
    for name in ("score_simulated_rides.py", "score_dated_rides.py", "replay_trip_analogues.py", "summarize_trip_analogues.py", "whole_journey_study.py",
                 "score_journey_control.py", "check_aiims_approach.py", "capture_near_departure.py", "capture_google_transit.py", "record_field_windows.py",
                 "export_field_evidence.py"):
        yield ROOT / "tools" / name
    yield ROOT / "tests" / "test_simulated_rides.py"
    yield ROOT / "tests" / "test_dated_ride_clocks.py"
    walk_replay = ROOT / "data" / "whole-journey-walk-check" / "control_replay.py"
    if walk_replay.is_file():
        yield walk_replay


def export(output=OUTPUT):
    verify_lock(STUDY)
    checks = {}
    for manifest, key in ((STUDY / "gps-simulation-protocol.json", "sha256"),
                          (ROOT / "data/whole-journey-control/punjabi-bagh/protocol.json", "protocol_sha256")):
        checks.update({ROOT / rel: h for rel, h in json.loads(manifest.read_text(encoding="utf-8"))[key].items()})
    sensitivity = json.loads((STUDY / "gps-first-route-number-sensitivity.json").read_text(encoding="utf-8"))
    checks[ROOT / sensitivity["scorer"]] = sensitivity["scorer_sha256"]
    checks[STUDY / "gps-simulation-protocol.json"] = sensitivity["primary_ledger_sha256"]
    for ledger in (STUDY / "near-departure").glob("*/status.json"):
        for row in json.loads(ledger.read_text(encoding="utf-8")).get("providers", {}).values():
            checks.update({ledger.parent / "capture" / rel: h for rel, h in row.get("raw_sha256", {}).items()})
    for path, expected in checks.items():
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f"Registered evidence changed in {path}")
    report = summary(field_results(ROOT / "data"))
    with tempfile.TemporaryDirectory(dir=output.parent) as tmp:
        pending = Path(tmp) / "evidence.zip"
        with zipfile.ZipFile(pending, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("Journey results.md", report)
            hashes = {"Journey results.md":hashlib.sha256(report.encode("utf-8")).hexdigest()}
            for path in evidence_files():
                data = path.read_bytes()
                name = path.relative_to(ROOT).as_posix()
                archive.writestr(name, data)
                hashes[name] = hashlib.sha256(data).hexdigest()
            archive.writestr("SHA256SUMS.txt", "".join(f"{h}  {name}\n" for name,h in hashes.items()))
        with zipfile.ZipFile(pending) as archive:
            bad = archive.testzip()
            if bad:
                raise ValueError(f"Invalid archived file {bad}")
        pending.replace(output)
    return output


if __name__ == "__main__":
    print(export())
