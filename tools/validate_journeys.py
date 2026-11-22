"""Compare public Google bus alternatives and selected observed stop pairs locally."""
import argparse
import csv
from collections import Counter, defaultdict
from datetime import datetime, time
import gzip
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from transit.eta import EtaModel, IST
from transit.router import Router
from transit.walking import Walking


def google_checks(router):
    results = []
    with (ROOT/"docs/google-observations.csv").open(encoding="utf-8", newline="") as source:
        for row in csv.DictReader(source):
            depart = datetime.fromisoformat(row["depart_ist"])
            result = router.journeys(f"{row['origin_lat']},{row['origin_lon']}",
                                     f"{row['destination_lat']},{row['destination_lon']}", depart)
            first = next(iter(result["journeys"]), None)
            end = datetime.combine(depart.date(), time.fromisoformat(row["shown_arrival_ist"]), IST)
            expected = int((end-depart).total_seconds())
            results.append({"origin":row["origin_name"], "destination":row["destination_name"],
                            "departure":row["depart_ist"], "google_seconds":expected,
                            "planner_seconds":first["duration_seconds"] if first else None,
                            "planner_routes":[leg["route"] for leg in first["legs"] if leg["mode"]=="bus"] if first else [],
                            "google_routes":row["bus_itinerary"], "warnings":result["warnings"]})
    return results


def observed_checks(router, model, data):
    """Select well-observed four-segment ODs by frequency only, then forecast at boarding."""
    with gzip.open(data/"eta_replay.json.gz", "rt", encoding="utf-8") as source:
        replay = json.load(source)
    # Identify successive stops on the same anonymised run. Retain only intervals
    # that passed the training pipeline's continuous-trace and plausibility checks.
    segments = {(row[1], row[2], row[3], row[4], row[5]) for row in replay["segments"]}
    events = defaultdict(list)
    for received, observed, route, run, stop in replay["passages"]:
        events[(route, run)].append((observed, stop))
    typical = {}
    for p in router.patterns:
        for i, (a,b) in enumerate(zip(p.stops, p.stops[1:])):
            typical.setdefault((p.route,a,b), p.arrivals[i+1] - p.arrivals[i])
    choices = defaultdict(list)
    for (route, _), rows in events.items():
        rows.sort()
        for i in range(len(rows)-4):
            chain = rows[i:i+5]
            stop_ids = tuple(stop for _,stop in chain)
            if len(set(stop_ids)) < 5:
                continue
            if all((tb,route,a,b,tb-ta) in segments and (route,a,b) in typical
                   for (ta,a),(tb,b) in zip(chain,chain[1:])):
                choices[(route,stop_ids)].append(chain)
    used_routes = set()
    out = []
    for (route, ids), chains in sorted(choices.items(), key=lambda item:(-len(item[1]),item[0])):
        if route in used_routes:
            continue
        used_routes.add(route)
        errors = Counter()
        for chain in chains:
            issue = chain[0][0]
            clock = datetime.fromtimestamp(issue, IST)
            clock_sec = clock.hour*3600 + clock.minute*60 + clock.second
            sums = Counter()
            for a,b in zip(ids,ids[1:]):
                baseline = typical[route,a,b]
                sums["schedule"] += baseline
                step, _ = model.duration(route,a,b,clock_sec+sums["model"],baseline,asof=issue)
                sums["model"] += step
            elapsed = chain[-1][0] - issue
            for key,s in sums.items():
                errors[key] += abs(s-elapsed)
        out.append({"route":router.routes[route][0], "origin":router.stops[ids[0]]["name"],
                    "destination":router.stops[ids[-1]]["name"], "traversals":len(chains),
                    "schedule_mae_s":round(errors["schedule"]/len(chains),1),
                    "model_mae_s":round(errors["model"]/len(chains),1)})
        if len(out)==5:
            break
    return out


def write_report(results, target):
    lines = [
        "# Delhi journey validation", "",
        "## Google Maps comparisons", "",
        "The queries used the same coordinates, local dates and departure times in both planners. In Google Maps the Bus preference was selected. The comparator is the earliest arrival among the displayed general bus itineraries. Durations below include waiting from the requested departure time. Google sometimes displays a later walking or boarding time as the start of an itinerary.", "",
        "| Origin → destination | Departure IST | Delhi Bus Atlas | Google Maps | Difference | Planner buses | Displayed Google buses |",
        "| --- | --- | ---: | ---: | ---: | --- | --- |",
    ]
    for item in results["google"]:
        actual = item["planner_seconds"]
        planned = f"{actual/60:.1f} min" if actual is not None else "No path"
        delta = f"{(actual-item['google_seconds'])/60:+.1f} min" if actual is not None else "—"
        lines.append(f"| {item['origin']} → {item['destination']} | {datetime.fromisoformat(item['departure']):%d %b %H:%M} | "
                     f"{planned} | {item['google_seconds']/60:.0f} min | {delta} | "
                     f"{' → '.join(item['planner_routes'])} | {item['google_routes']} |")
    lines.extend(["", "The observations were read from the Google Maps interface on 20 November 2026. Two are same-day retrospective estimates and one is a next-day estimate. These are comparisons of predictions. They do not establish the time any passenger actually arrived. Three pairs are too few to establish citywide door-to-door accuracy. Coordinates, displayed alternatives and the exclusion of a ladies-special service are recorded in [the observation log](google-observations.csv).", "",
        "## Observed bus journeys in the October holdout", "",
        "For each row the rider is assumed to be aboard at a known GPS-derived stop passage. We followed one vehicle run through four consecutive stop intervals, fixing the prediction clock at the first stop. The five examples below are selected by number of available traversals, with one example per route. The table reports mean absolute error across all matching traversals. These are in-vehicle accuracy checks on well-observed segments.", "",
        "| Route | Boarding stop → alighting stop | Traversals | Timetable error | Model error |",
        "| --- | --- | ---: | ---: | ---: |"])
    for item in results["observed"]:
        lines.append(f"| {item['route']} | {item['origin']} → {item['destination']} | {item['traversals']} | "
                     f"{item['schedule_mae_s']:.1f} s | {item['model_mae_s']:.1f} s |")
    lines.extend(["", "The full holdout includes routes with fewer observations and longer prediction horizons. [Weekday GPS evaluation](eta-validation.md) · [Walking coverage](walking.md)", "",
        "## Live integration check", "",
        "At 21:20:35 IST on 20 November, the public feed was 57 seconds old. The adapter matched 890 trips and accepted 12,242 stop predictions. A query from Libas Pur GT Road to GTK ByPass / Mubarak Chwok used live data for a 108-second wait and a 190-second ride on 193DOWN. Its 298-second total checks feed integration and time accounting. The bus's actual arrival was not observed.", "",
        "Run `python tools/validate_journeys.py` to reproduce the offline comparisons against the saved Google observations. The live check is a dated snapshot.", ""])
    target.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=ROOT/"data")
    args = parser.parse_args()
    model = EtaModel.load(args.data)
    router = Router(ROOT/"delhi_buses_static_gtfs_v1.zip", args.data, Walking(args.data), model)
    report = {"google":google_checks(router),"observed":observed_checks(router,model,args.data)}
    (args.data/"journey-validation.json").write_text(json.dumps(report,indent=2), encoding="utf-8")
    write_report(report, ROOT/"docs/journey-validation.md")
    print(json.dumps(report,indent=2,ensure_ascii=True))
