"""Compare predicted waits to the next observed bus at a fixed route and platform."""
import argparse
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime, timedelta
from functools import lru_cache
import json
from pathlib import Path
import subprocess
from statistics import mean, median
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from transit.eta import EtaModel, IST, read_data
from transit.router import Router
from transit.arrivals import approaches, boarding_predictions, upstream_prediction

LOOKAHEAD = 20 * 60


def platforms(router, profile, limit=12):
    # Select by training counts only. One boardable downstream platform per route.
    selected = {}
    for key, (stats, _) in sorted(profile.pairs.items(), key=lambda row: (-row[1][0][0], row[0])):
        route, _, stop = key.split("|")
        if route in selected:
            continue
        board = [(pid, i) for pid, i in router.at[stop] if router.patterns[pid].route == route
                 and not router.patterns[pid].pickups[i] and i < len(router.patterns[pid].stops) - 1]
        if board:
            selected[route] = (stop, board, stats[0])
        if len(selected) == limit:
            break
    return selected


def arrival_proxy(times, issued):
    index = bisect_left(times, issued)
    if index < len(times) and times[index] - issued <= LOOKAHEAD:
        return times[index] - issued
    return None


def model_wait(router, when, board):
    options = boarding_predictions(router, when, board)
    return min(wait for wait, _ in options) if options else None


def errors(rows, column):
    signed = [row[column] - row["observed_wait_s"] for row in rows]
    if not signed:
        return {"n": 0}
    absolute = sorted(map(abs, signed))
    return {"n": len(rows), "mae_s": round(mean(absolute), 1), "median_abs_s": median(absolute),
            "p90_abs_s": absolute[(9 * len(rows) + 9) // 10 - 1], "bias_s": round(mean(signed), 1)}


def evaluate(router, model, data, day_type):
    folder = data / "weekend" if day_type == "weekend" else data
    profile = model.weekend if day_type == "weekend" else model
    replay = read_data(folder / "eta_replay.json.gz")
    meta = json.loads((folder / "eta_metrics.json").read_text())
    return evaluate_windows(router, model, profile, replay, meta)


def evaluate_windows(router, model, profile, replay, meta):
    selected = platforms(router, profile)
    seen = defaultdict(set)
    # Require an incoming adjacent-stop traversal, to reject unconfirmed geofence entries.
    for _, observed, route, _, end, _ in replay["segments"]:
        if route in selected and end == selected[route][0]:
            seen[route].add(observed)
    clocks = {route:sorted(seen[route]) for route in selected}
    recent_passages = lru_cache(maxsize=None)(model.recent_passages)
    scored, per_platform = [], []
    attempted = no_observation = no_prediction = 0
    for route, (stop, board, training_count) in selected.items():
        samples = []
        active = router._active(datetime.fromisoformat(meta["holdout"]).date())
        paths = approaches(router, [(pid,i) for pid,i in board if router.patterns[pid].service in active])
        for start, end in meta["windows_ist"]:
            when = datetime.fromisoformat(meta["holdout"] + "T" + start).replace(tzinfo=IST) + timedelta(minutes=5)
            latest = datetime.fromisoformat(meta["holdout"] + "T" + end).replace(tzinfo=IST) - timedelta(seconds=LOOKAHEAD)
            while when <= latest:
                attempted += 1
                observed = arrival_proxy(clocks[route], int(when.timestamp()))
                if observed is None:
                    no_observation += 1
                else:
                    router.eta = None
                    schedule = model_wait(router, when, board)
                    router.eta = model
                    current = model_wait(router, when, board)
                    if schedule is None or current is None:
                        no_prediction += 1
                    else:
                        prediction = upstream_prediction(model, route, paths, when, recent_passages(when))
                        anchor = prediction[0] if prediction else None
                        samples.append({"issue_ist":when.isoformat(), "observed_wait_s":observed,
                                        "schedule_wait_s":schedule, "planner_wait_s":current,
                                        "upstream_wait_s":anchor,
                                        "upstream_or_planner_s":current if anchor is None else anchor})
                when += timedelta(minutes=5)
        scored.extend(samples)
        per_platform.append({"route":router.routes[route][0], "route_id":route, "stop_id":stop,
                             "stop_name":router.stops[stop]["name"], "training_pair_count":training_count,
                             "observed_instants":len(clocks[route]), "queries":samples})
    anchored = [row for row in scored if row["upstream_wait_s"] is not None]
    return {"date":meta["holdout"], "attempted":attempted, "no_observation_in_window":no_observation,
            "no_common_forecast":no_prediction, "scheduled":errors(scored, "schedule_wait_s"),
            "planner":errors(scored, "planner_wait_s"), "upstream_or_planner":errors(scored,"upstream_or_planner_s"),
            "when_anchored":{"schedule":errors(anchored,"schedule_wait_s"),"planner":errors(anchored,"planner_wait_s"),
                             "upstream":errors(anchored,"upstream_wait_s")}, "platforms":per_platform}


def confirm_october_23(router, directory, db_path):
    """Run the frozen weekday platform experiment on a later Friday."""
    from train_eta import extract, gtfs_routes

    day = "2026-10-23"
    if db_path.name != "bus_movements_2026_10_23.db":
        raise ValueError("Expected the Friday 23 October 2026 archive database.")
    fit = read_data(directory / "eta_model.json.gz")
    model = EtaModel(fit)
    selected = platforms(router, model)
    routes = gtfs_routes(ROOT / "delhi_buses_static_gtfs_v1.zip", selected)
    labels, passages, raw_count = extract(db_path, day, routes)
    replay = {"segments":[row[:6] for row in labels], "passages":passages}
    model = EtaModel(fit, replay)
    meta = {"holdout":day, "windows_ist":fit["metadata"]["windows_ist"]}
    output = evaluate_windows(router, model, model, replay, meta)
    output.update({"raw_pings_selected":raw_count, "training_dates":fit["metadata"]["train"],
                   "windows_ist":meta["windows_ist"], "selected_routes":len(routes)})
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=ROOT / "data")
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument("--confirmation-archive", type=Path, help="Run the independent 23 Oct platform-wait test on the original October 7z.")
    inputs.add_argument("--confirmation-db", type=Path, help="Use an already extracted 23 Oct database for the same test.")
    parser.add_argument("--sevenzip", type=Path, default=Path("C:/Program Files/7-Zip/7z.exe"))
    args = parser.parse_args()
    model = EtaModel.load(args.data)
    router = Router(ROOT / "delhi_buses_static_gtfs_v1.zip", args.data, None, model)
    if args.confirmation_archive or args.confirmation_db:
        if args.confirmation_db:
            confirmation = confirm_october_23(router, args.data, args.confirmation_db)
        else:
            db_name = "bus_movements_2026_10_23.db"
            with TemporaryDirectory(dir=ROOT, prefix=".wait-holdout-") as scratch:
                subprocess.run([str(args.sevenzip), "x", str(args.confirmation_archive.resolve()), "-o" + scratch, db_name],
                               stdout=subprocess.DEVNULL, check=True)
                confirmation = confirm_october_23(router, args.data, Path(scratch)/db_name)
        (args.data / "wait-confirmation.json").write_text(json.dumps(confirmation, indent=2), encoding="utf-8")
        print({k:v for k,v in confirmation.items() if k != "platforms"})
        sys.exit(0)
    report = {profile:evaluate(router, model, args.data, profile) for profile in ("weekday", "weekend")}
    (args.data / "wait-validation.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    for profile, result in report.items():
        print(profile, {key:value for key,value in result.items() if key != "platforms"})
