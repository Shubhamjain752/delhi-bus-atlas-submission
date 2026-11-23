"""Compare recent GPS history using the first-direct-bus study's fixed Delhi stop pairs."""
import argparse
from datetime import date, datetime
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import check_first_direct_bus as study
from tools.train_eta import boundary, format_clock, open_db
from transit.eta import EtaModel, IST
from transit.live import TTL
from transit.replay import CapturedOverlay
from transit.router import Router


# Chosen by clock and available complete databases, before inspecting the outcomes.
# Every window, including the 01:10 check, fits within one original daily database.
DAYS = ("2026-10-22", "2026-10-23", "2026-10-26", "2026-10-29")
HOURS = (1, 8, 13, 18)


def outcomes(rows):
    scores = study.aggregate(rows)
    result = {"queries": len(rows)}
    for endpoint in ("board", "exit"):
        c = scores[endpoint]["counts"]
        result[endpoint] = {
            "scored": c.get("scored", 0),
            "correct_4_minutes": c.get("old_correct", 0),
            "correct_30_minutes": c.get("new_correct", 0),
            "fixed": c.get("right_after_change", 0),
            "new_errors": c.get("wrong_after_change", 0),
        }
    for key, minutes in (("before", 4), ("after", 30)):
        result[f"picks_using_named_upstream_gps_{minutes}_minutes"] = sum(
            bool(r[key]) and r[key]["source"] == "upstream_gps" for r in rows)
    result["changed_top_line"] = scores["board"]["counts"].get("changed_all", 0)
    result["changed_boarding_time"] = scores["board"]["counts"].get("first_departure_time_changed", 0)
    return result


def evaluate(day, hour, dataset, db, model, router, registry):
    data = study.read(dataset / day / f"{hour:02}.gz")
    if data.get("truth_version") != 2:
        raise ValueError("Use the completed outcome pass of check_first_direct_bus.py.")
    local_day = date.fromisoformat(day)
    if any(m.last_train_day >= local_day for m in (model, model.weekend)):
        raise ValueError("Travel-time training must precede the evaluation date.")
    at = hour * 3600 + study.ISSUE_MIN * 60
    issue = study.issue_stamp(local_day, hour)
    when = datetime.fromtimestamp(issue, IST)
    ids = {r for _, _, route_ids in data["cases"] for r in route_ids}
    # Reuse the identical four-minute suffix in both arms. The additional slice
    # uses the same route filter and retains conflicting claims of the same vehicle.
    start_clock = format_clock(at - 1800)
    split_clock = format_clock(at - 240)
    low, split, high = (boundary(db, clock) for clock in (start_clock, split_clock, format_clock(at + 1)))
    prefix = study.past_rows(db, low, split, ids)
    suffix = study.past_rows(db, split, high, ids)
    if suffix != data["past"]:
        raise ValueError(f"Raw GPS and the frozen four-minute input differ at {day} {hour}:10.")
    if any(not start_clock <= row[0] < split_clock for row in prefix):
        raise ValueError("A receipt is outside the longer arm's additional time window.")
    projections = []
    for arm in (suffix, prefix + suffix):
        # restore rejects future observation times before constructing each poll.
        # The runtime applies its normal freshness checks to the accepted rows.
        overlay = study.restore(registry, model, issue - at, issue, arm)
        if any(not p["timestamp"] <= p["received_at"] <= issue for p in overlay.recent_passages(when)):
            raise ValueError("A GPS passage violates the issued forecast cutoff.")
        projections.append([
            study.pick(router, board, exit_stop, when, overlay, True)
            for board, exit_stop, _ in data["cases"]
        ])
    return [
        {"day": day, "hour": hour, "before": before, "after": after,
         "board": board, "exit": exit_stop, "board_winner": study.winner(witness, 0),
         "exit_winner": study.winner(witness, 1), "witnessed_lines": sorted(witness["complete"])}
        for (board, exit_stop, _), witness, before, after in zip(
            data["cases"], data["witnesses"], *projections, strict=True)
    ]


def runtime_signature():
    directory = Path(sys.modules[Router.__module__].__file__).parent
    payload = b"".join((directory / name).read_bytes() for name in (
        "router.py", "live.py", "eta.py", "arrivals.py", "replay.py"))
    return hashlib.sha256(payload).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--databases", type=Path, required=True, help="Four official daily SQLite files.")
    parser.add_argument("--panel", type=Path, required=True, help="Prepared first-direct-bus study directory.")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reference-runtime", help=(
        "Optional frozen first-direct-bus score signature. Verify that the four-minute "
        "result reproduces each saved top choice, time and source."))
    args = parser.parse_args()
    version = runtime_signature()
    model = EtaModel.load(ROOT / "data")
    router = Router(ROOT / "delhi_buses_static_gtfs_v1.zip", ROOT / "data", None, model)
    router._nearby = lru_cache(maxsize=50000)(router._nearby)
    registry = CapturedOverlay(model, ROOT / "data", 0).bind(router)
    rows = []
    reference_checked = 0
    for day in DAYS:
        source = args.databases / ("bus_movements_" + day.replace("-", "_") + ".db")
        saved = {}
        if args.reference_runtime:
            prior = study.read(args.panel / day / f"scored-native-{args.reference_runtime}.gz")
            saved = {(r["hour"], r["board"], r["exit"]): r["after"] for r in prior}
        with open_db(source) as db:
            for hour in HOURS:
                batch = evaluate(day, hour, args.panel, db, model, router, registry)
                if args.reference_runtime:
                    for row in batch:
                        if row["before"] != saved[hour, row["board"], row["exit"]]:
                            raise ValueError(f"Four-minute prediction differs from the specified reference "
                                             f"at {day} {hour}:10 for {row['board']}/{row['exit']}.")
                    reference_checked += len(batch)
                rows.extend(batch)
                print(f"{day} {hour:02}:10 finished", flush=True)
    if version != runtime_signature():
        raise ValueError("The production implementation changed during replay.")
    reference = json.loads((ROOT / "data/first-direct-bus.json").read_bytes())
    for path, digest in reference["timetable_and_models_sha256"].items():
        if hashlib.sha256((ROOT / path).read_bytes()).hexdigest() != digest:
            raise ValueError("The timetable or travel-time model differs from the registered study.")
    result = {
        "method": "Identical dated stop pairs, timetable, model and four-minute inputs. Only the longer arm also receives the preceding 26 minutes of vehicle-position polls. Both use named upstream positioning and ordinary destination ranking.",
        "dates": DAYS, "issue_clocks_ist": [f"{h:02}:10" for h in HOURS], "latest_fix_age_limit_seconds": TTL,
        "runtime_sha256": version, "four_minute_reference_runtime": args.reference_runtime,
        "four_minute_first_choices_verified": reference_checked,
        "timetable_and_models_sha256": reference["timetable_and_models_sha256"],
        "limits": "Four weekdays with complete available databases. GPS can verify vehicle passages on these links. Passenger boarding is unobserved. These cases overlap the prior six-day evaluation and do not establish accuracy on new dates.",
        "all": outcomes(rows),
        "by_time_ist": {f"{h:02}:10": outcomes([r for r in rows if r["hour"] == h]) for h in HOURS},
        "by_date": {d: outcomes([r for r in rows if r["day"] == d]) for d in DAYS},
    }
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["all"]))


if __name__ == "__main__":
    main()
