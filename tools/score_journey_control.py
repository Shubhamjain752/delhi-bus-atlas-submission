"""Score the registered Punjabi Bagh control against the dated Sunday GPS receipts."""
import argparse
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.score_simulated_rides import epoch, fingerprint, run, stamp
from tools.whole_journey_study import read

CONTROL = ROOT / "data" / "whole-journey-control" / "punjabi-bagh"
REPORT = ROOT / "docs" / "whole-journey-control.md"
ARMS = ("atlas", "google", "google-159")
LABELS = {"atlas":"Atlas 159UP", "google":"Google 938A", "google-159":"Google 159, matched line"}
HORIZON = 90


def load_scenarios():
    protocol = read(CONTROL / "protocol.json")
    for relative, expected in protocol["protocol_sha256"].items():
        if fingerprint(ROOT / relative) != expected:
            raise ValueError(f"Frozen control input changed: {relative}")
    plan = read(CONTROL.parent / "plan.json")["cases"]["punjabi-bagh"]
    scenarios = {}
    for arm in ARMS:
        scenario = read(CONTROL / "simulation" / f"{arm}-scenario.json")
        if (scenario["arm"] != arm or scenario["case"] != "punjabi-bagh"
                or any(scenario[k] != v for k,v in plan.items())
                or not epoch(scenario["forecast_capture"]) <= epoch(protocol["registered"]) < epoch(plan["departure"])):
            raise ValueError(f"Control query or capture timing mismatch for {arm}")
        scenarios[arm] = scenario
    # The pinned scorer uses these labels only to print a Google marker event.
    # They are absent from the original JSON. Route IDs and marker coordinates
    # come exclusively from the registered scenario.
    scenarios["google"]["legs"][0]["display_route"] = read(CONTROL / "google" / "prediction.json")["earliest"]["legs"][0]["route"]
    scenarios["google-159"]["legs"][0]["display_route"] = read(CONTROL / "matched-line.json")["google_bus"]
    return scenarios


def atomic_text(path, text):
    pending = tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False, mode="w", encoding="utf-8")
    tmp = Path(pending.name)
    try:
        with pending:
            pending.write(text)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def interval_error(forecast, bounds):
    # Positive = the predicted arrival is earlier than the reconstructed arrival.
    lower, upper = [epoch(value) - epoch(forecast) for value in bounds]
    if lower <= 0 <= upper:
        return "Within arrival interval"
    if lower > 0:
        return f"{lower}–{upper} s early"
    return f"{-upper}–{-lower} s late"


def duration(seconds):
    return f"{seconds//60}m {seconds%60:02}s"


def default_sim(result):
    return next((sim for sim in result.get("simulations", [])
                 if sim["walking_pace_mps"] == 1.25 and sim["transfer_allowance_seconds"] == 0), {})


STATUS = {
    "no_later_witnessed_boarding":"No qualifying bus entry observed after the rider was ready",
    "same_bus_alighting_not_witnessed":"First qualifying bus seen boarding-side only",
    "boarding_interval_overlaps_readiness":"First bus could have passed before the rider was ready",
    "ordering_of_first_two_buses_unresolved":"Order of the first two buses is uncertain",
    "first_approaching_run_has_ambiguous_branch":"Could not resolve the branch of the first bus",
    "unresolved_route_to_map_landmarks":"Route could not be aligned to the registered poles",
    "no_independently_mapped_walk_to_platform":"No independently mapped walk to the pole",
    "no_independently_mapped_final_walk":"No independently mapped final walk",
    "independent_walk_grossly_circuitous":"Mapped walk to the pole is circuitous",
    "independent_final_walk_grossly_circuitous":"Mapped final walk is circuitous",
    "complete_simulation":"Reconstructed from dated GPS",
}


def describe(result, arm, file):
    sim = default_sim(result)
    status = sim.get("status")
    bounds = sim.get("arrival_interval_ist")
    status_text = STATUS.get(status, status or "No dated GPS receipts")
    if status == "complete_simulation":
        status_text += " " + "–".join(v[11:19] for v in bounds)
        parts = " / ".join(duration(sim[k]) for k in ("walking_seconds", "waiting_seconds", "riding_seconds"))
        delta = interval_error(result["scenario"]["forecast_arrival"], bounds)
    else:
        parts, delta = "—", "Unscored"
    board = next((e for e in sim.get("events", []) if e["kind"] == "board"), None)
    if board and status != "complete_simulation":
        status_text += f" ({board['lower'][11:19]}–{board['upper'][11:19]})"
    return (f"| [{LABELS[arm]}](../data/whole-journey-control/punjabi-bagh/simulation/{file}) | "
            f"{status_text} | {parts} | {delta} |")


def diagnostics(result):
    departure = epoch(result["scenario"]["departure"])
    candidates = [c for leg in result.get("gps_passages_by_leg", []) for c in leg
                  if c["pattern_status"] == "serves" and epoch(c["board"]["upper"]) >= departure]
    first = "No qualifying entry"
    if candidates:
        first = " / ".join(("–".join(hit[k][11:19] for k in ("lower", "upper")) if hit else "unseen")
                           for hit in (candidates[0]["board"], candidates[0]["alight"]))
    count = str(len(candidates)) + " / " + str(sum(c["alight"] is not None for c in candidates))
    crossing = result.get("supplemental_directed_shape_crossings", {})
    strict_published = result.get("with_published_distances_for_unmapped_walks", {})
    crossing_published = crossing.get("with_published_distances_for_unmapped_walks", {})
    def evidence(method):
        sim = default_sim(method)
        if not sim:
            return "—"
        if sim["status"] == "complete_simulation":
            interval = "–".join(t[11:19] for t in sim["arrival_interval_ist"])
            components = " / ".join(duration(sim[k]) for k in ("walking_seconds","waiting_seconds","riding_seconds"))
            return f"{interval} ({components})"
        return STATUS.get(sim["status"], sim["status"])
    return first, count, [evidence(method) for method in (crossing, strict_published, crossing_published)]


def update_report(results):
    result = results["atlas"]
    window = result["observation"]
    source = result["source"]
    final = window["state"] == "horizon_exhausted"
    state = "Final 90-minute observation window" if final else "Interim observation"
    lines = [f"**{state}.** GPS receipt cutoff {window['receipt_cutoff_ist'][11:19]} IST. "
             f"{source['captured_snapshots']} archived feed snapshots.", ""]
    if source["captured_snapshots"]:
        lines += [f"First receipt {source['first_receipt'][11:19]}, last receipt {source['last_receipt'][11:19]}. "
                  f"{source['fresh_headers']} passed the two-minute feed-freshness filter. "
                  f"{len(source['snapshot_gaps_over_120s'])} gaps between fresh receipts exceeded two minutes.", ""]
    lines += ["| Forecast | Default GPS reconstruction | Modelled walk / wait / observed ride midpoint | Forecast error |",
              "| --- | --- | --- | --- |"]
    suffix = "result" if final else "interim"
    for arm, obj in results.items():
        lines.append(describe(obj, arm, f"{arm}-{suffix}.json"))
    lines += ["", "The primary table compares the saved clock reading to the arrival interval. Google displays only the minute.",
              "", "### GPS at the two poles", "",
              "| Selected route | Boarding entries / entries also seen alighting-side | Earliest boarding passage / same bus at the exit pole (IST) |",
              "| --- | --- | --- |"]
    for name, obj in results.items():
        first, count, _ = diagnostics(obj)
        lines.append(f"| {LABELS[name]} | {count} | {first} |")
    lines += ["", "These counts include strictly matched entries whose boarding interval ends at or after 15:10. "
              "A bus passing a pole can be observed even when the preceding walk or a passenger boarding cannot be established.",
              "", "### Sensitivity checks", "",
              "| Selected route | Allow a directed crossing between fixes | Substitute published distance for the missing walk | Both changes |",
              "| --- | --- | --- | --- |"]
    for name, obj in results.items():
        _, _, sensitivity = diagnostics(obj)
        lines.append("| " + " | ".join([LABELS[name], *sensitivity]) + " |")
    lines += ["", "Where a sensitivity completes the route, the cell gives destination arrival and the estimated walk / wait / ride midpoint. "
              "Google's boarding marker is about nine metres from the origin pin. The pedestrian graph has no path between the two. "
              "The published-distance sensitivity assumes zero access metres because Google starts the itinerary at its boarding pole. "
              "It uses the independently mapped 52-metre final walk. "
              "Each linked result also includes slower and faster walking paces."]
    old = REPORT.read_text(encoding="utf-8")
    before, marker, tail = old.partition("<!-- outcomes:start -->")
    _, end, after = tail.partition("<!-- outcomes:end -->")
    if not marker or not end:
        raise ValueError("The control report has no outcomes insertion points")
    atomic_text(REPORT, before + marker + "\n\n" + "\n".join(lines) + "\n\n" + end + after)


def scan(scenarios, source, cutoff, end):
    departure = epoch(scenarios["atlas"]["departure"])
    if not source.is_dir():
        raise FileNotFoundError(f"Original Sunday GPS receipt folder is unavailable: {source}")
    final = cutoff >= end
    until = min(cutoff, end)
    files = sorted(p for p in source.glob("positions-*.pb.gz")
                   if departure-1800 <= int(p.name.split("-")[1]) <= until)
    results = {}
    # Each arm must see precisely the same snapshot list, even while the
    # recorder publishes new receipts. Reuse its finished immutable files.
    with tempfile.TemporaryDirectory(prefix="delhi-control-") as temp:
        sample = Path(temp)
        for f in files:
            try:
                os.link(f, sample / f.name)
            except OSError:
                shutil.copyfile(f, sample / f.name)
        for name, scenario in scenarios.items():
            result = run(scenario, sample, HORIZON) if files else {
                "scenario":scenario, "source":{"type":"captured_vehicle_positions","captured_snapshots":0,"files":{}}, "simulations":[]}
            result["observation"] = {"state":"horizon_exhausted" if final else "interim",
                                     "receipt_cutoff_ist":stamp(until), "horizon_end_ist":stamp(end),
                                     "horizon_minutes":HORIZON,
                                     "registered_scenario_sha256":fingerprint(CONTROL / "simulation" / f"{name}-scenario.json")}
            results[name] = result
    suffix = "result" if final else "interim"
    for name, result in results.items():
        existing = CONTROL / "simulation" / f"{name}-{suffix}.json"
        if existing.exists() and not read(existing)["source"]["files"].items() <= result["source"]["files"].items():
            raise ValueError("New sample omits or changes previously saved GPS receipts")
    for name, result in results.items():
        output = CONTROL / "simulation" / f"{name}-{suffix}.json"
        atomic_text(output, json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        print(describe(result, name, output.name), flush=True)
    update_report(results)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="One due scan. Default waits and refreshes through the horizon.")
    parser.add_argument("--source", type=Path, help="Directory of original dated vehicle-position receipts")
    args = parser.parse_args()
    scenarios = load_scenarios()
    departure = datetime.fromisoformat(scenarios["atlas"]["departure"])
    source = args.source or ROOT / ".whole-journey-witness" / departure.date().isoformat()
    start = int((departure + timedelta(minutes=11)).timestamp())
    end = int((departure + timedelta(minutes=HORIZON)).timestamp())
    # Leave in-flight atomic file writes a minute to land at the closing edge.
    last_scan = end + 60
    while True:
        now = int(time.time())
        if now >= start and (now < end or now >= last_scan):
            print("Receipt cutoff", stamp(min(now,end)), flush=True)
            scan(scenarios, source, now, end)
            if now >= last_scan:
                return
        if args.once:
            return
        due = start if now < start else min(last_scan, now + 300)
        time.sleep(max(1, due-time.time()))


if __name__ == "__main__":
    main()
