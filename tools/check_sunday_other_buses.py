"""Recheck the dated corridor using the other lines on Google's saved screen.

Install requirements-build.txt; run this script with Python 3.12. It reads the
original GPS receipts, verifies their saved hashes and prints the results.
"""
from datetime import date
from pathlib import Path
import csv
import io
import json
import re
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import score_simulated_rides as score

directory = ROOT / "data/whole-journey/sunday-offpeak"
capture = directory / "capture/google-transcription.json"
frozen = directory / "simulation/google-result.json"
saved = json.loads(frozen.read_text(encoding="utf-8"))
scenario = saved["scenario"]
lines = {"433", "507E", "323", "479", "724A", "711"}
options = json.loads(capture.read_text(encoding="utf-8"))["displayed_options_from_capture"]
assert all(any("Bus " + number + '"' in option or "Bus " + number + " " in option
               for option in options) for number in lines)
schedule = ROOT / "delhi_buses_static_gtfs_v1.zip"
with zipfile.ZipFile(schedule) as archive:
    name = next(name for name in archive.namelist() if name.rsplit("/", 1)[-1] == "routes.txt")
    routes = csv.DictReader(io.TextIOWrapper(archive.open(name), encoding="utf-8-sig"))
    ids = {row["route_id"] for row in routes if any(
        re.sub(r"(UP|DOWN)$", "", row[key].strip().upper()) in lines
        for key in ("route_short_name", "route_long_name"))}
start = score.epoch(scenario["departure"])
hashes = saved["source"]["files"]
files = [ROOT / ".whole-journey-witness/2026-11-22" / name for name in sorted(hashes)
         if start - 120 <= int(name.split("-")[1]) <= start + 3700]
assert all(score.fingerprint(path) == hashes[path.name] for path in files)
tracks, counts = score.collect(score.live_positions(files, ids))
router = score.Router(schedule, ROOT / "data", None, None)
leg = {**scenario["legs"][0], "route_ids": sorted(ids)}
result, diagnostics, aligned = score.passage_candidates(
    router, [leg], tracks, date.fromisoformat(scenario["departure"][:10]),
    scenario["landmarks"], use_crossings=False)
rows = []
for hit in sorted(result[0], key=lambda h: h["board"]["upper"]):
    if hit["board"]["upper"] < start or hit["board"]["lower"] > start + 42*60:
        continue
    record = {k: hit[k] for k in ("route", "route_id", "run_token", "pattern_status")}
    for key in ("board", "alight"):
        event = hit[key]
        record[key] = {t: score.stamp(event[t]) for t in ("lower", "upper")} if event else None
    rows.append(record)

# Google supplied detailed markers only for 433. Keep each marker distinct from
# the nearest scheduled platform on the other observed route.
day = date.fromisoformat(scenario["departure"][:10])
route_479 = {h["route_id"] for h in rows if h["route"] == "479UP"}
platforms = {}
for patterns in score.all_active(router, route_479, day).values():
    for pattern in patterns:
        ends = []
        indices = []
        for role in ("board", "alight"):
            marker = scenario["landmarks"][leg[role]]
            flags = pattern.pickups if role == "board" else pattern.drops
            distances = [(score.approximate_m(marker, score.coords(router.stops[sid])), n, sid)
                         for n, sid in enumerate(pattern.stops) if flags[n] == 0]
            if not distances:
                break
            metres, n, sid = min(distances)
            indices.append(n)
            ends.append({"stop_id": sid, "name": router.stops[sid]["name"],
                         "marker_distance_metres": round(metres, 1)})
        if len(ends) == 2:
            platforms[tuple(e["stop_id"] for e in ends)] = {
                "board": ends[0], "alight": ends[1], "forward_order": indices[1] > indices[0]}

official_passages = []
for (a, b), pair in platforms.items():
    if not pair["forward_order"]:
        continue
    candidate = {"route_ids": sorted(route_479), "board": a, "alight": b}
    matched_tokens = {r["run_token"] for r in rows if r["route"] == "479UP"}
    route_tracks = {run: points for run, points in tracks.items() if run[0] in route_479}
    for crossings in (False, True):
        matched, _, _ = score.passage_candidates(router, [candidate], route_tracks, day, {}, crossings)
        for event in matched[0]:
            if event["run_token"] not in matched_tokens:
                continue
            detail = {"run_token": event["run_token"], "board_id": a, "alight_id": b,
                      "crossing_inference_enabled": crossings, "pattern_status": event["pattern_status"]}
            for location in ("board", "alight"):
                e = event[location]
                detail[location] = ({"lower": score.stamp(e["lower"]), "upper": score.stamp(e["upper"]),
                                     "evidence": e["evidence"]} if e else None)
            official_passages.append(detail)

atlas_path = directory / "capture/atlas.json"
atlas = json.loads(atlas_path.read_text(encoding="utf-8"))
last_walk = atlas["journeys"][0]["legs"][-1]
assert last_walk["mode"] == "walk" and [
    last_walk["to"]["lat"], last_walk["to"]["lon"]] == scenario["destination"]
alternatives = []
for hit in official_passages:
    if (hit["crossing_inference_enabled"] or hit["pattern_status"] != "serves"
            or not hit["alight"] or hit["alight_id"] != last_walk["from"]["id"]):
        continue
    alternatives.append({
        "route": "479UP",
        "condition": "Passenger reaches official AIIMS Ring Road stop 2839 before the boarding interval and can board. The 4-minute 300 m approach is a scenario for Google's pin, 7 m from this stop. A longer crossing route may miss the bus.",
        "at_boarding_stop_before": hit["board"]["lower"],
        "source_of_final_walk": str(atlas_path.relative_to(ROOT).as_posix()),
        "sha256_of_saved_atlas_forecast": score.fingerprint(atlas_path),
        "walk_from": last_walk["from"]["name"], "walk_from_stop_id": last_walk["from"]["id"],
        "modelled_final_walk_metres": last_walk["distance_m"],
        "modelled_final_walk_seconds": last_walk["seconds"],
        "conditional_arrival_at_destination_pin": [
            score.stamp(score.epoch(hit["alight"][bound]) + last_walk["seconds"])
            for bound in ("lower", "upper")],
        "status": "Exploratory alternative on recorded stop-area passages with a modelled last walk. No passenger was timed."
    })
print(json.dumps({
    "type": "Exploratory audit of direct line numbers at previously saved 433 markers",
    "date": scenario["departure"], "frozen_google_capture": capture.relative_to(ROOT).as_posix(),
    "frozen_google_capture_sha256": score.fingerprint(capture),
    "frozen_scoring_result": frozen.relative_to(ROOT).as_posix(),
    "frozen_scoring_result_sha256": score.fingerprint(frozen),
    "official_schedule_sha256": score.fingerprint(schedule),
    "modelled_first_walk_seconds": 240, "modelled_last_walk_seconds": 152,
    "markers": scenario["landmarks"], "passage_rule": "outside to inside 45 metres, with the dated direction checked against both marker positions on the published route shape",
    "inputs": "Data from same date; continuous same-vehicle GPS through both stop areas for full passage.",
    "screen_lines": sorted(lines), "source_snapshot_count": len(files),
    "capture_window": [score.stamp(start - 120), score.stamp(start + 3700)],
    "boarding_window": [score.stamp(start), score.stamp(start + 42*60)],
    "comparison": "For a passenger ready at 15:04 (or Google's 15:05), look for other captured direct lines before the later 433, observed at 15:41:20 to 15:41:53. An overlapping readiness interval or missing exit observation leaves boarding or completion unresolved.",
    "route_ids_with_compatible_geometry": aligned[0],
    "screening_diagnostics": diagnostics, "observed_passages": rows,
    "nearest_official_479up_platforms_by_active_pattern": list(platforms.values()),
    "same_479up_at_its_published_stops": official_passages,
    "conditional_479up_journey_using_official_stops": alternatives,
    "limitations": "Only 433's Google stops were captured in detail. Alternative line numbers were screened against those two markers, then 479UP was separately checked against its own official stops. A bus near a marker need not open its doors. Earlier services may be missing. Route 507E has no exact number in the official feed."
}, indent=2))
