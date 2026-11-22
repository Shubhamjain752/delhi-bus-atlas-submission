"""Publish a completed, cross-checked month of Delhi boarding decisions."""
import argparse
from collections import Counter
import csv
from datetime import date, datetime
import gzip
import hashlib
import io
import json
from pathlib import Path
from statistics import median
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.near_miss_confirmation import policies
from tools.near_miss_peak import LABELS, count_labels
from tools.train_eta import gtfs_routes
from transit.eta import IST

MONTH = [f"2026-10-{i:02d}" for i in range(1, 32)]
TRAINING = {10, 11, 12, 13}
PREVIOUS = {15, 17, 22, 23, 26, 29}
PERIODS = {"AM": "08:00–09:30", "noon": "12:00–13:30", "PM": "17:00–18:30", "night": "22:00–23:30"}


def date_group(day):
    d = date.fromisoformat(day).day
    if d in TRAINING:
        return "training"
    if d in PREVIOUS:
        return "previous"
    return "pretraining" if d < min(TRAINING) else "fresh"


def check_day(day, item, manifest):
    def require(condition, message):
        if not condition:
            raise ValueError(f"{day}: {message}")

    require(item["complete"] and item["day"] == day and item["signature"] == manifest["signature"],
            "wrong date, incomplete output or mixed protocol versions")
    require(item["archive_member"] == manifest["archive_members"][day], "source archive member changed")
    rows = item["opportunities"]
    require(len({r["bus"] for r in rows}) == len(rows), "a vehicle/day appears more than once")
    require(all(r["day"] == day and r["peak"] in PERIODS and r["outcome"] in LABELS and
                r["origin_gps"] <= r["forecast_receipt"] < r["ready"] and
                r["ready"] == r["forecast"] + r["gap"] for r in rows),
            "invalid as-of cutoff, label, or forecast clock")
    c = item["census"]
    require(c["raw_records"] == sum(c["by_route"].values()) == sum(c["by_hour"].values()),
            "whole-day receipt counts do not reconcile")
    expected = {r["run"]: r for r in rows if 1 <= r["gap"] <= 30}
    paired = item["router"]["rows"]
    require(len(paired) == len(expected) and {r["run"] for r in paired} == set(expected),
            "eligible opportunities and router attempts differ")
    require(set(item["windows"]) == set(PERIODS), "missing a study window")
    require(sum(w["opportunities"] for w in item["coverage"].values()) == len(rows),
            "window totals and sampled opportunities differ")
    for r in paired:
        src = expected[r["run"]]
        require(all(r[k] == src[k] for k in ("day", "bus", "route_id", "peak", "stop", "next_stop", "gap", "ready", "outcome")),
                "the paired router result changed the case or ground-truth label")
        require(not r.get("conditional_emitted") or r.get("baseline_conditional_emitted"),
                "the exit check introduced an offer")
    measured = policies(paired)
    for policy, table in [("baseline", item["router"]["baseline"]["overall"]), ("revised", item["router"]["overall"])]:
        require(measured[policy] == {"offered": table["conditional_emitted"], **table["emitted_outcomes"]},
                "published router outcomes differ from per-case results")
    if not item["profile"]["learned_profile_available"]:
        require(all(r["forecast_source"] == "timetable" for r in rows), "future fitted times leaked into early dates")


def summarize(opportunities, attempts):
    selected = policies(attempts)
    active = [r for r in attempts if r.get("forecast_accepted_at_issue")]
    gaps = [r["same_platform_fallback_seconds"] - r["conditional_seconds"] for r in attempts if r.get("conditional_emitted")]
    return {
        "opportunities": len(opportunities), "positive_gap_1_30": len(attempts),
        "matched": sum("static_trip" in r for r in attempts), "reconstructed_calls": len(active),
        "distinct_buses": len({r["bus"] for r in opportunities}),
        "observed_routes": len({r["route_id"] for r in opportunities}),
        "offered_routes": len({r["route_id"] for r in attempts if r.get("conditional_emitted")}),
        "attempted_outcomes": count_labels(attempts), "accepted_outcomes": count_labels(active),
        "exclusions": dict(Counter(r["excluded"] for r in attempts if "excluded" in r)),
        "forecast_sources": dict(Counter(r["forecast_source"] for r in opportunities)),
        "approaching_without_offer": sum(r["outcome"] == "approaching" and not r.get("conditional_emitted") for r in attempts),
        "offered_same_platform_fallback_extra_seconds": (
            {"min": min(gaps), "median": median(gaps), "max": max(gaps)} if gaps else None),
        "policies": selected,
    }


def examples(rows, opportunities, static, audits):
    with zipfile.ZipFile(static) as z, z.open("stops.txt") as f:
        names = {r["stop_id"]: r["stop_name"] for r in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))}
    # Fixed selection order gives examples from the fresh split, then other dates if necessary.
    ordered = sorted(rows, key=lambda r: (date_group(r["day"]) != "fresh", r["ready"], r["run"]))
    patterns = [("withheld_approach", False, "approaching"), ("withheld_departed", False, "gone"),
                ("retained_departed", True, "gone"), ("retained_approach", True, "approaching")]
    result = []
    for kind, retained, label in patterns:
        case = next((r for r in ordered if r.get("baseline_conditional_emitted") and
                     bool(r.get("conditional_emitted")) == retained and r["outcome"] == label), None)
        if case is None:
            continue
        src = opportunities[case["day"], case["run"]]
        ready = datetime.fromtimestamp(src["ready"], IST)
        platform = names.get(src["stop"], src["stop"])
        description = f"At {platform}, the forecast had passed by {src['gap']} seconds. "
        audit = audits.get((case["day"], case["run"]))
        if audit and audit.get("exit_received") is not None:
            delta = audit["exit_received"] - src["ready"]
            description += ("The GPS exit arrived at the rider's ready time." if delta == 0 else
                            f"The GPS exit arrived {abs(delta)} seconds {'after' if delta > 0 else 'before'} the rider was ready.")
        elif label == "approaching":
            description += "A later GPS trace placed the bus upstream when the rider was ready."
        else:
            description += "GPS placed the bus beyond the platform. The exit message receipt time was unavailable."
        result.append({"kind": kind, "day": src["day"], "route": src["route"], "period": src["peak"],
                       "ready": ready.isoformat(), "platform": platform, "description": description,
                       "outcome": label, "offered": retained})
    return result


def approach_timing(rows, timetable, route_ids):
    """Compare both one-segment forecasts against the same recorded bus passages."""
    scheduled = {}
    for route, (stops, times, counts, _, _) in gtfs_routes(timetable, route_ids).items():
        for a, b, duration in zip(stops, stops[1:], times):
            if counts[a] == counts[b] == 1:
                scheduled[route, a, b] = max(1, duration)

    def score(cases):
        sample = [(r, r["gps_entry_exit"]) for r in cases if r["gps_entry_exit"] is not None]
        result = {"opportunities": len(cases), "verified_stop_entries": len(sample),
                  "unverified": len(cases) - len(sample)}
        for name in ("planner", "anchored_timetable"):
            errors, minimum_errors, widths = [], [], []
            for r, (outside, inside, _) in sample:
                pred = (r["forecast"] if name == "planner" else
                        r["origin_gps"] + scheduled[r["route_id"], r["upstream"], r["stop"]])
                errors.append(pred - inside)
                minimum_errors.append(max(outside - pred, pred - inside, 0))
                widths.append(inside - outside)
            if errors:
                result["median_sampling_interval_seconds"] = median(widths)
                result[name] = {"mean_absolute_error_seconds": round(sum(map(abs, errors)) / len(errors), 2),
                                "median_absolute_error_seconds": median(map(abs, errors)),
                                "median_signed_error_seconds": median(errors),
                                "within_60_seconds": sum(abs(v) <= 60 for v in errors),
                                "mean_minimum_error_to_gps_interval_seconds": round(sum(minimum_errors) / len(errors), 2)}
        return result
    return {"all_dates": score(rows), "fresh": score([r for r in rows if date_group(r["day"]) == "fresh"]),
            "fresh_by_day_type": {typ: score([r for r in rows if date_group(r["day"]) == "fresh" and
                                      (date.fromisoformat(r["day"]).weekday() >= 5) == (typ == "weekend")])
                                  for typ in ("weekday", "weekend")}}


def build(directory):
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if set(manifest["archive_members"]) != set(MONTH):
        raise ValueError("The archive inventory must contain all 31 October days.")
    signature = dict(manifest)
    del signature["signature"]
    if hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest() != manifest["signature"]:
        raise ValueError("The stored month manifest checksum is invalid")
    for name, digest in manifest["source_hashes"].items():
        with (ROOT / name).open("rb") as src:
            if hashlib.file_digest(src, "sha256").hexdigest() != digest:
                raise ValueError(f"{name} differs from the frozen month experiment")
    all_cases, all_attempts, audits, by_day = [], [], [], {}
    hours, routes, sample_windows, fresh_windows = Counter(), Counter(), Counter(), Counter()
    missing_hours, empty_windows = {}, {}
    for day in MONTH:
        with gzip.open(directory / (day + ".json.gz"), "rt", encoding="utf-8") as f:
            item = json.load(f)
        check_day(day, item, manifest)
        cases, attempts = item["opportunities"], item["router"]["rows"]
        all_cases.extend(cases)
        all_attempts.extend(attempts)
        audits.extend(item["router"].get("gone_offer_receipt_audit", []))
        c = item["census"]
        hours.update(c["by_hour"])
        routes.update(c["by_route"])
        absent = [f"{i:02d}" for i in range(24) if not c["by_hour"].get(f"{i:02d}", 0)]
        if absent:
            missing_hours[day] = absent
        empty = [k for k, v in item["coverage"].items() if not v["opportunities"]]
        if empty:
            empty_windows[day] = empty
        sample_windows.update({k: v["raw_rows"] for k, v in item["coverage"].items()})
        fresh_windows.update({k: v["fresh_unique_pings"] for k, v in item["coverage"].items()})
        by_day[day] = {**summarize(cases, attempts), "group": date_group(day), "profile": item["profile"],
                       "raw_records": c["raw_records"], "recorded_receipt_hours": len(set(c["by_hour"]) - {"unknown"}),
                       "first_receipt_clock": c["first_receipt_clock"], "last_receipt_clock": c["last_receipt_clock"]}
    def filtered(check):
        return summarize([r for r in all_cases if check(r)], [r for r in all_attempts if check(r)])

    groups = {}
    for name in ["fresh", "previous", "training", "pretraining"]:
        dates = [d for d in MONTH if date_group(d) == name]
        groups[name] = {"dates": dates, **filtered(lambda r: r["day"] in dates)}
    total = summarize(all_cases, all_attempts)
    selected_routes = set(manifest["routes"])
    static = ROOT / "delhi_buses_static_gtfs_v1.zip"
    with zipfile.ZipFile(static) as feed, feed.open("routes.txt") as f:
        official_routes = {r["route_id"] for r in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))}
    reported_routes = set(routes) - {"missing"}
    by_route = {route: {"name": name, "static_agency": agency,
                        **filtered(lambda r: r["route_id"] == route)}
                for route, (name, agency) in manifest["routes"].items()}
    case_index = {(r["day"], r["run"]): r for r in all_cases}
    audit_index = {(r["day"], r["run"]): r for r in audits}
    return {
        "status": "complete", "schema": 1, "signature": manifest["signature"],
        "calendar": {"first": MONTH[0], "last": MONTH[-1], "expected_days": len(MONTH), "processed_days": len(by_day)},
        "census": {"raw_records": sum(routes.values()), "route_ids": len(reported_routes),
                   "reported_route_ids_in_official_schedule": len(reported_routes & official_routes),
                   "reported_route_ids_absent_from_schedule": len(reported_routes - official_routes),
                   "records_with_official_route_id": sum(routes[r] for r in reported_routes & official_routes),
                   "selected_route_ids_with_records": len(reported_routes & selected_routes),
                   "missing_route_records": routes["missing"], "selected_route_records_all_hours": sum(routes[r] for r in selected_routes),
                   "calendar_hour_bins": 24 * len(MONTH),
                   "hour_bins_with_records": 24 * len(MONTH) - sum(map(len, missing_hours.values())),
                   "hours_without_a_record": missing_hours, "windows_with_no_eligible_decision": empty_windows,
                   "by_receipt_hour": dict(sorted(hours.items())), "window_scan_raw_records_including_context": dict(sample_windows),
                   "window_scan_fresh_distinct_coordinate_records_including_context": dict(fresh_windows),
                   "uncompressed_bytes": sum(m["bytes"] for m in manifest["archive_members"].values())},
        "scope": {"route_selection": "40 routes retained from the original volume-ranked study, 10 additional fixed-hash timetable routes, and both directions of 502.",
                  "periods": PERIODS, "day_unit": "Each physical bus is assigned to one window and can contribute at most one opportunity per date.",
                  "forecast_source": "Reconstructed from an upstream GPS receipt and the running-time prior available on the date. October has no original provider stop forecasts or door events."},
        "cohort": {k: v for k, v in total.items() if k != "policies"}, "policies": total["policies"],
        "groups": groups, "by_day": by_day, "by_period": {p: filtered(lambda r: r["peak"] == p) for p in PERIODS},
        "by_day_type": {typ: filtered(lambda r: (date.fromisoformat(r["day"]).weekday() >= 5) == (typ == "weekend"))
                        for typ in ["weekday", "weekend"]},
        "by_route_group": {g: filtered(lambda r: r["route_group"] == g) for g in manifest["route_groups"]},
        "by_static_agency": {agency: filtered(lambda r: manifest["routes"][r["route_id"]][1] == agency)
                             for agency in sorted({values[1] for values in manifest["routes"].values()})},
        "by_gap_seconds": {f"{lo}-{hi}": filtered(lambda r: lo <= r["gap"] <= hi) for lo, hi in [(1, 5), (6, 15), (16, 30)]},
        "exact_three_seconds": {"all_dates": filtered(lambda r: r["gap"] == 3),
                                "fresh": filtered(lambda r: r["gap"] == 3 and date_group(r["day"]) == "fresh")},
        "by_route": by_route,
        "approach_timing": approach_timing(all_cases, static, selected_routes),
        "exit_evidence": {"baseline_gone_audited": len(audits),
                          "entry_and_exit_received_in_time": sum(a["both_received_by_ready"] is True for a in audits),
                          "entry_or_exit_received_late": sum(a["both_received_by_ready"] is False for a in audits),
                          "receipt_unverified": sum(a["both_received_by_ready"] is None for a in audits),
                          "retained_departed_with_late_receipt": sum(not a["suppressed"] and a["both_received_by_ready"] is False for a in audits),
                          "retained_departed_with_timely_receipts": sum(not a["suppressed"] and a["both_received_by_ready"] is True for a in audits),
                          "confirmed_gone_suppressed": total["policies"]["suppressed"]["gone"]},
        "examples": examples(all_attempts, case_index, static, audit_index),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=ROOT / "data" / "month-boarding")
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "month-boarding.json")
    args = parser.parse_args()
    report = build(args.directory)
    pending = args.output.with_suffix(".pending")
    pending.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    pending.replace(args.output)
