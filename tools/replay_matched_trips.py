"""Replay the public forecasts actually received by each November comparison minute."""
import csv
from datetime import datetime
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from transit.eta import EtaModel, IST
from transit.arrivals import platform_estimate
from transit.replay import load_november_capture, received_files
from transit.router import Router
from transit.walking import Walking
from transit.walking_option import walking_alternative


def main():
    data = ROOT / "data"
    samples = data / "nov21-samples"
    passages = json.loads((samples / "passages-review.json").read_text(encoding="utf-8"))["candidates"]
    with (ROOT / "docs" / "matched-google-observations.csv").open(encoding="utf-8", newline="") as src:
        google_cases = [r for r in csv.DictReader(src) if r["group"] == "primary"]
    model = EtaModel.load(data)
    router = Router(ROOT / "delhi_buses_static_gtfs_v1.zip", data, Walking(data), model)
    cases = [("285", "289", "1459", "01:17"), ("2383", "2386", "1996", "01:18"), ("8065","3472","2106","01:20")]
    report = []
    baseline = json.loads((data / "nov21-baseline.json").read_text(encoding="utf-8"))
    for origin, end, route, clock in cases:
        issue = datetime.fromisoformat("2026-11-21T" + clock).replace(tzinfo=IST)
        issue_epoch = int(issue.timestamp())
        overlay, recording = load_november_capture(router, model, data, issue_epoch)
        inputs = [path.name for receipt, _, path in received_files(samples) if receipt <= issue_epoch]
        locations = [(router.stops[s]["lat"],router.stops[s]["lon"]) for s in (origin,end)]
        reply = router.journeys(*locations, issue, asof=issue, eta=overlay)
        walk = walking_alternative(router.walking, reply)
        status = overlay.status(asof=issue_epoch)
        board = platform_estimate(router, overlay, origin, route, issue)
        options = [{"arrival":j["arrival"], "duration_seconds":j["duration_seconds"], "walk_seconds":j["walk_seconds"],
                    "wait_seconds":j["wait_seconds"], "ride_seconds":j["ride_seconds"],
                    "legs":[{key:leg[key] for key in ("mode","seconds","route","trip_id","live_run_id","arrival","source") if key in leg}
                            for leg in j["legs"]]} for j in reply["journeys"]]
        row = {"origin":router.stops[origin], "destination":router.stops[end], "issue":issue.isoformat(),
               "inputs_received":inputs, "replay":recording, "matched_forecasts_at_issue":status["matched_stop_updates"],
               "options":options, "platform_estimate":board, "warnings":reply["warnings"],
               "boarding_alerts":reply["boarding_alerts"]}
        passage, = [p for p in passages if p["route_id"] == route and
                    (p["events"][0]["id"], p["events"][-1]["id"]) == (origin, end) and
                    datetime.fromisoformat(p["events"][0]["gps_ist"]).replace(second=0) == issue]
        first, last = passage["events"][0], passage["events"][-1]
        reading, = [g for g in google_cases if (g["origin_stop_id"], g["destination_stop_id"]) == (origin, end)
                    and g["checked_date_ist"] == issue.date().isoformat() and g["requested_departure_ist"] == clock]
        if [(float(reading["origin_lat"]),float(reading["origin_lon"])),
            (float(reading["destination_lat"]),float(reading["destination_lon"]))] != locations:
            raise ValueError(f"Google coordinates disagree with GTFS stops {origin} and {end}")
        google_arrival = datetime.fromisoformat(reading["checked_date_ist"] + "T" + reading["final_arrival_ist"]).replace(tzinfo=IST)
        row["observed"] = {"route":passage["route_name"], "origin_passage_ist":first["gps_ist"],
                           "destination_passage_ist":last["gps_ist"],
                           "elapsed_from_request_seconds":int(last["gps_time"] - issue.timestamp())}
        row["google"] = {"route":reading["google_route"], "final_arrival_ist":google_arrival.isoformat(),
                         "boarding_ist":datetime.fromisoformat(reading["checked_date_ist"]+"T"+reading["bus_boards_ist"]).replace(tzinfo=IST).isoformat(),
                         "elapsed_from_request_seconds":int((google_arrival - issue).total_seconds()),
                         "url":reading["exact_time_url"]}
        previous, = [c for c in baseline["cases"] if (c["origin_id"],c["destination_id"],c["issue"]) ==
                     (origin,end,issue.isoformat())]
        row["original_forecast"] = {"arrival":previous["arrival"],"duration_seconds":previous["duration_seconds"],
                                    "wait_seconds":previous["wait_seconds"],"release_sha256":baseline["release_sha256"]}
        row["walking_alternative"] = ({k:walk[k] for k in ("seconds","distance_m","arrival","time_saved_seconds","reason")}
                                      if walk else None)
        report.append(row)
        conditional = [a["conditional_itinerary"]["arrival"] for a in reply["boarding_alerts"] if a.get("conditional_itinerary")]
        print(origin, end, clock, "feeds", len(inputs), "feasible", options[0]["arrival"], "conditional", conditional)
    (data / "nov21-planner-replay.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
