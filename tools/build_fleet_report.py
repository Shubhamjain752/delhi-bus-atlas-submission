"""Reconcile all-route GPS screening, October source census and paired planner decisions."""
import argparse
from collections import Counter
import csv
from datetime import date, datetime, timedelta, timezone
import gzip
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import zipfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from transit.schedule import cache_schedule
DATES = tuple(f"2026-10-{day:02}" for day in range(1, 32))
GROUPS = ("all", "development", "holdout", "overnight", "holdout_overnight")
LABELS = ("approaching", "gone", "uncertain", "unverified")
IST = timezone(timedelta(hours=5, minutes=30))


def empty_counts():
    return {
        "cases": 0, "matched": 0, "replayed": 0,
        "outcomes_screened": dict.fromkeys(LABELS, 0),
        "outcomes_matched": dict.fromkeys(LABELS, 0),
        "outcomes_replayed": dict.fromkeys(LABELS, 0),
        **{key: dict.fromkeys(("offered", *LABELS), 0)
           for key in ("baseline", "revised", "removed", "added", "revised_default", "revised_secondary")},
    }


def count_case(counts, case):
    label = case["outcome"]
    counts["cases"] += 1
    counts["outcomes_screened"][label] += 1
    if case["eligibility"] is None:
        counts["matched"] += 1
        counts["outcomes_matched"][label] += 1
    if not case["selected"]:
        return
    counts["replayed"] += 1
    counts["outcomes_replayed"][label] += 1
    before, after = case["baseline"]["offered"], case["revised"]["offered"]
    foreground = after and case["revised"].get("vehicle_approaching") is True
    for key, include in (("baseline", before), ("revised", after), ("removed", before and not after),
                         ("added", after and not before), ("revised_default", foreground),
                         ("revised_secondary", after and not foreground)):
        if include:
            counts[key]["offered"] += 1
            counts[key][label] += 1


def case_groups(day, hour):
    held_out = day >= DATES[23]
    keys = ["all", "holdout" if held_out else "development"]
    if hour < 6:
        keys.append("overnight")
        if held_out:
            keys.append("holdout_overnight")
    return keys


def last_service_departure(data):
    cache = cache_schedule(ROOT / "delhi_buses_static_gtfs_v1.zip", data)
    db = sqlite3.connect(cache.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        # Router.max_service uses the last sorted start plus its pattern's final departure offset.
        return db.execute("SELECT max(json_extract(trips,'$[#-1][0]') + json_extract(departures,'$[#-1]')) FROM patterns").fetchone()[0]
    finally:
        db.close()


def empty_service_days():
    def subset():
        return {"civil_dates": set(), "routes": set(), **empty_counts()}
    return {"matched": 0, "civil_dates": set(), "same_service_day": 0, "next_service_day": 0,
            "previous": subset(), "previous_after_static_end": subset()}


def count_service_day(group, case, last_departure):
    if case["eligibility"] is not None:
        return
    day, service = case["day"], case["service_day"]
    group["matched"] += 1
    group["civil_dates"].add(day)
    if service == day:
        group["same_service_day"] += 1
        return
    offset = (date.fromisoformat(service) - date.fromisoformat(day)).days
    if offset == 1:
        group["next_service_day"] += 1
        return
    if offset != -1:
        raise ValueError("Dated trip falls outside the supported service-day search")
    clock = datetime.fromtimestamp(case["ready"], IST)
    seconds = clock.hour * 3600 + clock.minute * 60 + clock.second
    subsets = [group["previous"]]
    if seconds + 86400 > last_departure:
        subsets.append(group["previous_after_static_end"])
    for subset in subsets:
        subset["civil_dates"].add(day)
        subset["routes"].add(case["route_id"])
        count_case(subset, case)


def finish_service_days(groups, service_groups):
    for name, s in service_groups.items():
        if s["matched"] != groups[name]["matched"] or s["matched"] != s["same_service_day"] + s["next_service_day"] + s["previous"]["matched"]:
            raise ValueError("Service-day partitions disagree with full matched cohort")
        s["civil_dates"] = sorted(s["civil_dates"])
        for key in ("previous", "previous_after_static_end"):
            s[key]["civil_dates"] = sorted(s[key]["civil_dates"])
            s[key]["routes"] = sorted(s[key]["routes"])


def validate_case(case, allowed_days):
    day, hour, route, label = case["day"], case["hour"], case["route_id"], case["outcome"]
    if day not in allowed_days or type(hour) is not int or not 0 <= hour < 24 or not route or label not in LABELS:
        raise ValueError(f"Unexpected date, hour, route or GPS outcome in {day} {route} {hour}")
    clock = datetime.fromtimestamp(case["ready"], IST)
    if clock.date().isoformat() != day or clock.hour != hour:
        raise ValueError("Simulated readiness and Delhi date/hour disagree")
    if type(case["selected"]) is not bool:
        raise ValueError("Missing replay selection flag")
    if case["selected"] and case["eligibility"] is not None:
        raise ValueError("An excluded case entered the matched planner cohort")
    for key in ("baseline", "revised"):
        decision = case.get(key)
        if case["selected"]:
            if not isinstance(decision, dict) or type(decision.get("offered")) is not bool:
                raise ValueError(f"Selected decision is missing a {key} result")
        elif decision is not None:
            raise ValueError("An unselected case has a planner result")


def read_json(path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as source:
        return json.load(source)


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def static_names(path):
    with zipfile.ZipFile(path) as archive:
        def table(name):
            return csv.DictReader(io.TextIOWrapper(archive.open(name), encoding="utf-8-sig"))
        routes = {r["route_id"]: (r["route_short_name"] or r["route_long_name"], r["agency_id"])
                  for r in table("routes.txt")}
        stops = {r["stop_id"]: r["stop_name"] for r in table("stops.txt")}
    return routes, stops


def union_with_short_routes(short_path, by_route, official):
    short = read_json(short_path)
    if not short["complete"] or set(short["days"]) != set(DATES):
        raise ValueError("The short-route check is incomplete")
    raw = set(by_route)
    matched = {rid for rid, r in by_route.items() if r["matched"]}
    definitive = {rid for rid, r in by_route.items() if r["outcomes_matched"]["approaching"] + r["outcomes_matched"]["gone"]}
    short_rows = short["routes"]
    short_raw = {rid for rid, r in short_rows.items() if r["raw"]}
    issued = {rid for rid, r in short_rows.items() if r["issued"]}
    verified = {rid for rid, r in short_rows.items() if r["model"]["verified_entries"]}
    if not (verified <= issued <= short_raw <= raw) or not set(short_rows) <= set(official):
        raise ValueError("Short-route GPS coverage disagrees with archive or timetable IDs")
    if any(by_route[rid]["raw_records"] != short_rows[rid]["raw"] for rid in short_raw):
        raise ValueError("The main and short-route checks disagree on route GPS counts")
    official_seen = set(official) & raw
    stages = {
        "raw_reported": raw, "official_observed": official_seen, "named_matched": matched,
        "short_official": set(short_rows), "short_observed": short_raw, "short_issued": issued,
        "short_verified": verified, "named_and_short_issued": matched & issued, "named_and_short_verified": matched & verified,
        "short_issued_beyond_named": issued - matched, "short_verified_beyond_named": verified - matched,
        "named_or_short_issued": matched | issued, "named_or_short_verified": matched | verified,
        "named_definitive": definitive, "named_definitive_or_short_verified": definitive | verified,
        "short_observed_without_named_or_short_issue": short_raw - matched - issued,
        "other_official_observed_without_named_or_short_issue": official_seen - short_raw - matched - issued,
        "observed_without_named_or_short_issue": raw - matched - issued,
        "official_observed_without_named_or_short_issue": official_seen - matched - issued,
        "official_no_gps": set(official) - raw,
    }
    return {"route_counts": {k: len(ids) for k, ids in stages.items()}, "short_report_sha256": digest(short_path),
            "additional_issued_ids": sorted(issued - matched), "additional_verified_ids": sorted(verified - matched),
            "overlap_issued_ids": sorted(matched & issued), "uncovered_official_observed_ids": sorted(official_seen - matched - issued)}


def coverage(old_directory):
    """Independent all-message census from the previous scan of the same 31 source members."""
    route_records, hour_records = Counter(), Counter()
    daily = {}
    for day in DATES:
        evidence = read_json(old_directory / (day + ".json.gz"))
        c = evidence["census"]
        if (not evidence["complete"] or evidence["day"] != day or
                c["raw_records"] != sum(c["by_hour"].values()) or c["raw_records"] != sum(c["by_route"].values())):
            raise ValueError("Invalid archived all-message census: " + day)
        route_records.update(c["by_route"])
        hour_records.update({int(h): n for h, n in c["by_hour"].items()})
        daily[day] = {"raw_records": c["raw_records"], "by_hour": c["by_hour"],
                      "first_receipt": c["first_receipt_clock"], "last_receipt": c["last_receipt_clock"]}
    return route_records, hour_records, daily


def case_key(r):
    return r["day"], str(r["route_id"]), r["hour"]


def reconcile_screen(directory, expected, selected, daily, by_route, by_hour, diagnostic):
    """Match every cell and sampled decision back to its source; retain the counting unit at each stage."""
    fields = ["raw", "valid", "unique_fixes", "forward_stop_forecasts", "near_miss_opportunities"]
    manifest = read_json(directory / "manifest.json")
    signature = manifest.pop("signature")
    if signature != hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest():
        raise ValueError("Fleet extraction manifest signature mismatch")
    if manifest["cell_fields"] != fields:
        raise ValueError("Unexpected GPS screening stages")
    archive = read_json(directory.parent / "month-boarding" / "manifest.json")["archive_members"]
    selected_by_key = {case_key(c): c for c in selected}
    source_count = 0
    totals = Counter()
    receipt_cells, forecast_cells, candidate_cells = 0, 0, 0
    checked, rescued, exhausted = 0, 0, 0
    rescued_reasons = Counter()
    route_fields = {r: [0] * len(fields) for r in by_route}
    hour_fields = [[0] * len(fields) for _ in by_hour]
    for day in DATES:
        day_data = read_json(directory / (day + ".json.gz"))
        if (day_data["day"] != day or day_data["complete"] is not True or len(day_data["hours"]) != 24
                or day_data["signature"] != signature or day_data["crc32"] != archive[day]["crc32"]
                or day_data["cell_fields"] != fields):
            raise ValueError("Incomplete, wrong-source or mismatched GPS day: " + day)
        keys = {}
        for hour, cells in enumerate(day_data["hours"]):
            raw = 0
            for route, values in cells.items():
                if route not in route_fields or len(values) != len(fields) or any(type(v) is not int or v < 0 for v in values):
                    raise ValueError("Unexpected per-cell GPS metrics")
                raw += values[0]
                if values[0] < values[1] or values[1] < values[2] or values[3] < values[4]:
                    raise ValueError("Inconsistent receipt or decision funnel")
                receipt_cells += values[0] > 0
                forecast_cells += values[3] > 0
                if values[4]:
                    keys[day, route, hour] = values[4]
                    candidate_cells += 1
                for i, value in enumerate(values):
                    route_fields[route][i] += value
                    hour_fields[hour][i] += value
                    totals[fields[i]] += value
                if hour == 1 and by_route[route]["route"] == "502UP":
                    for field, value in zip(fields, values):
                        diagnostic["gps"][field] += value
            if raw != daily[day]["by_hour"].get(f"{hour:02}", 0):
                raise ValueError(f"Raw GPS totals disagree across independent scans for {day} {hour:02}")
        for case in day_data["cases"]:
            key = case_key(case)
            if key not in keys or key not in expected:
                raise ValueError(f"GPS cell and planner cohort disagree at {key}")
            opportunities = keys.pop(key)
            n = case["candidates_checked"]
            eligible = case["eligible_named_case"]
            if (type(eligible) is not bool or type(n) is not int or not 1 <= n <= opportunities
                    or (not eligible and n != opportunities)):
                raise ValueError(f"Candidate search ended before confirming eligibility or exhausting the cell: {key}")
            checked += n
            exhausted += not eligible
            if eligible and n > 1:
                rescued += 1
                rescued_reasons[case["first_candidate_exclusion"]] += 1
            if expected[key] != (case["ready"], str(case["stop"]), case["outcome"], eligible):
                raise ValueError(f"GPS readiness, platform or outcome changed in replay at {key}")
            source_count += 1
            if key in selected_by_key:
                replay = selected_by_key[key]
                evidence = case["gps_entry_exit_receipts"]
                replay["exit_receipt_delta_seconds"] = (evidence[2] - case["ready"]
                                                      if evidence and evidence[2] is not None else None)
                replay["entry_receipt_delta_seconds"] = (evidence[1] - case["ready"]
                                                       if evidence and evidence[1] is not None else None)
        if keys:
            raise ValueError(f"{len(keys)} positive route-hours missing from GPS sample on {day}")
    if source_count != len(expected):
        raise ValueError("A planner case has no original GPS anchor")
    for route, values in route_fields.items():
        if values[0] != by_route[route]["raw_records"]:
            raise ValueError("The independent raw route censuses disagree for " + route)
        by_route[route]["gps"] = dict(zip(fields, values))
    for hour, values in enumerate(hour_fields):
        by_hour[hour]["gps"] = dict(zip(fields, values))
    return {"protocol": manifest["protocol"], "signature": signature, "source_archive_sha256": manifest["source_archive_sha256"],
            "calendar_route_hour_cells_with_receipts": receipt_cells, "cells_with_forecasts": forecast_cells,
            "cells_with_borderline_candidate": candidate_cells, "candidates_checked_for_eligibility": checked,
            "cells_rescued_by_later_candidate": rescued, "cells_exhausted_without_match": exhausted,
            "rescued_first_candidate_exclusions": dict(rescued_reasons), "stages": dict(totals)}


def timing_of_gone_offers(cases):
    """Distinguish a confirmation received too late from a usable positive departure observation."""
    audit = {g: {p: Counter() for p in ("baseline", "revised", "revised_default")} for g in GROUPS}
    for case in cases:
        if case["outcome"] != "gone":
            continue
        entry, exit_ = case["entry_receipt_delta_seconds"], case["exit_receipt_delta_seconds"]
        state = ("missing_receipt_bracket" if entry is None or exit_ is None else
                 "entry_or_exit_received_late" if max(entry, exit_) > 0 else "both_received_by_ready")
        for group in case_groups(case["day"], case["hour"]):
            for policy in ("baseline", "revised", "revised_default"):
                offered = (case["revised"]["offered"] and case["revised"].get("vehicle_approaching") is True
                           if policy == "revised_default" else case[policy]["offered"])
                if offered:
                    audit[group][policy][state] += 1
    return audit


def approach_age_band(age):
    if type(age) is not int or not 0 <= age <= 60:
        raise ValueError("A positive approach requires a GPS observation aged 0–60 seconds")
    return "0_15" if age <= 15 else "16_30" if age <= 30 else "31_60"


def platform_summary(data, groups, seen, selected):
    source = data / "fleet-platform.json"
    published = read_json(source)
    if published["complete"] is not True or published["matched"] != groups["all"]["matched"]:
        raise ValueError("Missing full-cohort platform audit")
    ages = {band: dict.fromkeys(LABELS, 0) for band in ("0_15", "16_30", "31_60")}
    holdout_hours = [dict.fromkeys(LABELS, 0) for _ in range(24)]
    holdout_routes = {}
    fields = ("before_exit", "after_exit", "new_exit", "lost_exit", "revised_approach",
              "unconfirmed", "exit_overrides_approach")
    checked_groups = {g: {"matched": 0, **{s: dict.fromkeys(LABELS, 0) for s in fields}} for g in GROUPS}
    offered = {case_key(c): c["revised"] for c in selected if c["revised"]["offered"]}
    unique = set()
    for day in DATES:
        daily = read_json(data / "fleet-platform" / (day + ".json.gz"))
        if daily["day"] != day or daily["signature"] != published["signature"]:
            raise ValueError("Wrong day or rule version in platform audit")
        for rid, hour, ready, label, before, after, approach, age in daily["rows"]:
            key = day, str(rid), hour
            ref = seen.get(key)
            if (not ref or ref[0] != ready or ref[2] != label or not ref[3] or key in unique or
                    any(type(v) is not bool for v in (before, after, approach))):
                raise ValueError(f"Platform row differs from named-trip cohort at {key}")
            unique.add(key)
            signal = approach and not after
            states = (before, after, after and not before, before and not after, signal,
                      not after and not approach, after and approach)
            for group in case_groups(day, hour):
                counts = checked_groups[group]
                counts["matched"] += 1
                for field, active in zip(fields, states):
                    if active:
                        counts[field][label] += 1
            if signal:
                band = approach_age_band(age)
                if day >= DATES[23]:
                    ages[band][label] += 1
                    holdout_hours[hour][label] += 1
                    if key[1] not in holdout_routes:
                        holdout_routes[key[1]] = dict.fromkeys(LABELS, 0)
                    holdout_routes[key[1]][label] += 1
            if key in offered:
                actual = offered.pop(key)
                if after or actual["vehicle_approaching"] != approach or actual["gps_age_seconds"] != age:
                    raise ValueError(f"Platform classifier differs from production alert at {key}")
    if (offered or checked_groups != published["groups"] or
            published["actual_offer_parity_checks"] != groups["all"]["revised"]["offered"]):
        raise ValueError("Incomplete comparison between independent platform checks and planner cases")
    for name, table in checked_groups.items():
        if groups[name]["matched"] != table["matched"]:
            raise ValueError("Matched route-hour cohort differs in " + name)
    for rows in (ages.values(), holdout_hours, holdout_routes.values()):
        if any(sum(b[k] for b in rows) != checked_groups["holdout"]["revised_approach"][k] for k in LABELS):
            raise ValueError("Held-out GPS signal breakdown differs from matched cohort")
    return {"signature": published["signature"], "sha256": digest(source), "groups": checked_groups,
            "actual_offer_parity_checks": published["actual_offer_parity_checks"], "holdout_approach_by_age": ages,
            "holdout_approach_by_hour": holdout_hours, "holdout_approach_by_route": holdout_routes}


def verify_selection(cases, routes):
    eligible = [c for c in cases if c["eligibility"] is None]
    selected = [c for c in cases if c["selected"]]
    for extract in (lambda r: (r["route_id"], r["hour"] < 6), lambda r: (r["day"], r["hour"])):
        if {extract(r) for r in eligible} != {extract(r) for r in selected}:
            raise ValueError("Paired test omitted a route or date/hour with an eligible case")
    def diagnostics(rows):
        return {(r["route_id"], r["hour"]) for r in rows if routes[r["route_id"]][0] in ("502UP", "502DOWN")}
    if diagnostics(eligible) != diagnostics(selected):
        raise ValueError("A testable hour of route 502 is missing from paired replay")


def select_examples(cases, routes, stops):
    # Prefer cases from the date split fixed for this revision. Use first by time within each type.
    ordered = sorted(cases, key=lambda r: (r["day"] < DATES[23], r["ready"], str(r["route_id"])))
    conditions = (
        ("overnight_gone", lambda c: c["hour"] < 6 and c["revised"]["offered"] and c["outcome"] == "gone"),
        ("retained_gone", lambda c: c["revised"]["offered"] and c["outcome"] == "gone"),
        ("removed_approach", lambda c: c["baseline"]["offered"] and not c["revised"]["offered"] and c["outcome"] == "approaching"),
        ("added_gone", lambda c: not c["baseline"]["offered"] and c["revised"]["offered"] and c["outcome"] == "gone"),
        ("withheld_gone", lambda c: c["baseline"]["offered"] and not c["revised"]["offered"] and c["outcome"] == "gone"),
        ("added_approach", lambda c: not c["baseline"]["offered"] and c["revised"]["offered"] and c["outcome"] == "approaching"),
        ("retained_approach", lambda c: c["revised"]["offered"] and c["outcome"] == "approaching"),
        ("offered_uncertain", lambda c: c["revised"]["offered"] and c["outcome"] in ("uncertain", "unverified")),
        ("default_gone", lambda c: c["revised"]["offered"] and c["revised"].get("vehicle_approaching") is True and c["outcome"] == "gone"),
        ("secondary_gone", lambda c: c["revised"]["offered"] and c["revised"].get("vehicle_approaching") is not True and c["outcome"] == "gone"),
        ("secondary_approach", lambda c: c["revised"]["offered"] and c["revised"].get("vehicle_approaching") is not True and c["outcome"] == "approaching"),
    )
    used, examples = set(), []
    for kind, condition in conditions:
        row = next((r for r in ordered if case_key(r) not in used and condition(r)), None)
        if row is None:
            continue
        used.add(case_key(row))
        rid, stop = str(row["route_id"]), str(row["stop"])
        examples.append({"kind": kind, "day": row["day"], "hour": row["hour"], "route_id": rid,
                         "route": routes[rid][0], "stop": stop, "stop_name": stops.get(stop, stop),
                         "ready_ist": datetime.fromtimestamp(row["ready"], IST).isoformat(),
                         "outcome": row["outcome"], "baseline": row["baseline"]["offered"],
                         "revised": row["revised"]["offered"], "reason": row["revised"].get("reason"),
                         "gps_age_seconds": row["revised"].get("gps_age_seconds"),
                         "vehicle_approaching": row["revised"].get("vehicle_approaching"),
                         "entry_receipt_delta_seconds": row.get("entry_receipt_delta_seconds"),
                         "exit_receipt_delta_seconds": row.get("exit_receipt_delta_seconds"),
                         "baseline_reason": row["baseline"].get("reason")})
    return examples


def build(data):
    routes, stops = static_names(ROOT / "delhi_buses_static_gtfs_v1.zip")
    route_records, hour_records, daily = coverage(data / "month-boarding")
    groups = {key: empty_counts() for key in GROUPS}
    last_departure = last_service_departure(data)
    service_groups = {key: empty_service_days() for key in GROUPS}
    by_route = {rid: {"route_id": rid, "route": routes.get(rid, (None, None))[0],
                      "operator": routes.get(rid, (None, None))[1], "raw_records": n, **empty_counts()}
                for rid, n in route_records.items()}
    by_hour = [{"hour": h, "raw_records": hour_records[h], **empty_counts()} for h in range(24)]
    route_at_one = empty_counts()
    route_at_one["gps"] = Counter()
    by_day = {day: {**c, **empty_counts()} for day, c in daily.items()}
    by_operator = {}
    for r in by_route.values():
        operator = r["operator"] or "unmatched"
        group = by_operator.setdefault(operator, {"raw_records": 0, "reported_routes": 0, **empty_counts()})
        group["raw_records"] += r["raw_records"]
        group["reported_routes"] += 1
    excluded = Counter()
    for row in [*by_route.values(), *by_hour]:
        row["exclusions"] = Counter()
    reasons = {g: {p: Counter() for p in ("baseline", "revised")} for g in GROUPS}
    selected, seen, selection, hashes = [], {}, {}, {}
    for split, days in (("dev", DATES[:23]), ("holdout", DATES[23:])):
        source = data / "fleet-router" / (split + ".json.gz")
        replay = read_json(source)
        hashes[split] = digest(source)
        selection[split] = replay["selection"]
        if list(days) != selection[split]["dates"] or len(replay["cases"]) != selection[split]["screened"]:
            raise ValueError("The split dates or published case count changed for " + split)
        verify_selection(replay["cases"], routes)
        for case in replay["cases"]:
            validate_case(case, days)
            day, rid, hour = key = case_key(case)
            if key in seen or rid not in by_route:
                raise ValueError(f"Duplicate case or route missing from source census: {key}")
            seen[key] = case["ready"], str(case["stop"]), case["outcome"], case["eligibility"] is None
            if case["eligibility"] is not None:
                excluded[case["eligibility"]] += 1
                by_route[rid]["exclusions"][case["eligibility"]] += 1
                by_hour[hour]["exclusions"][case["eligibility"]] += 1
            keys = case_groups(day, hour)
            for g in keys:
                count_case(groups[g], case)
                count_service_day(service_groups[g], case, last_departure)
                if case["selected"]:
                    for p in ("baseline", "revised"):
                        reasons[g][p][case[p].get("reason") or ("offered" if case[p]["offered"] else "unspecified")] += 1
            route = by_route[rid]
            for group in (route, by_hour[hour], by_day[day], by_operator[route["operator"] or "unmatched"]):
                count_case(group, case)
            if route["route"] == "502UP" and hour == 1:
                count_case(route_at_one, case)
            if case["selected"]:
                selected.append(case)
        del replay
    for k in ("cases", "matched", "replayed"):
        if groups["all"][k] != sum(r[k] for r in by_route.values()) or groups["all"][k] != sum(r[k] for r in by_hour):
            raise ValueError("Route or hour totals disagree")
    totals = {"raw_records": sum(route_records.values()), "reported_routes": len(route_records),
              "official_observed_routes": len(route_records.keys() & routes.keys()),
              "official_routes_in_feed": len(routes), "routes_with_candidate": sum(r["cases"] > 0 for r in by_route.values()),
              "routes_with_matched_case": sum(r["matched"] > 0 for r in by_route.values()),
              "routes_replayed": sum(r["replayed"] > 0 for r in by_route.values()),
              "official_stops": len(stops), "boarding_stops_with_candidate": len({x[1] for x in seen.values()}),
              "boarding_stops_replayed": len({str(r["stop"]) for r in selected}),
              "calendar_hours_with_receipts": sum(n > 0 for row in daily.values() for n in row["by_hour"].values()),
              "calendar_hours_with_case": len({(d, h) for d, _, h in seen}),
              "calendar_hours_replayed": len({(r["day"], r["hour"]) for r in selected})}
    screen = reconcile_screen(data / "fleet-cases", seen, selected, daily, by_route, by_hour, route_at_one)
    platform = platform_summary(data, groups, seen, selected)
    finish_service_days(groups, service_groups)
    totals["routes_with_valid_gps"] = sum(r["gps"]["valid"] > 0 for r in by_route.values())
    totals["routes_with_stop_forecasts"] = sum(r["gps"]["forward_stop_forecasts"] > 0 for r in by_route.values())
    return {"schema_version": 1, "complete": True, "totals": totals, "groups": groups, "by_hour": by_hour,
            "by_route": sorted(by_route.values(), key=lambda r: (r["route"] or "~", r["route_id"])),
            "by_operator": by_operator, "by_day": by_day, "route_502up_hour_01": route_at_one,
            "exclusions": dict(excluded), "replay_reasons": reasons, "screen": screen, "selection": selection,
            "platform": platform,
            "route_coverage_union": union_with_short_routes(data / "fleet-short-links.json", by_route, routes),
            "service_days": {"last_scheduled_departure_seconds": last_departure,
                             "next_civil_day_cutoff_seconds": last_departure - 86400,
                             "groups": service_groups},
            "router_input_sha256": hashes, "examples": select_examples(selected, routes, stops),
            "gone_offer_receipts": timing_of_gone_offers(selected),
            "methods": {"timezone": "Asia/Kolkata", "development_dates": list(DATES[:23]), "holdout_dates": list(DATES[23:]),
                        "overnight": "00:00 inclusive through 06:00 exclusive, by rider readiness",
                        "unit": "one borderline case per route, civil date and hour, taking the first eligible named trip or retaining the first excluded case after all candidates fail",
                        "outcome": "later positions on the same GPS stream; doors and passenger boarding are unobserved",
                        "weighting": "selected cases, without passenger or ridership weights",
                        "forecast": "stop forecast reconstructed from upstream GPS and scheduled running time",
                        "paired": "earlier and revised planner tested on the same stratified subset of matched cases"}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=ROOT / "data")
    args = parser.parse_args()
    result = build(args.data)
    path = args.data / "fleet-boarding.json"
    pending = path.with_suffix(".pending")
    pending.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False), encoding="utf-8")
    pending.replace(path)
    print(path, result["totals"])


if __name__ == "__main__":
    main()
