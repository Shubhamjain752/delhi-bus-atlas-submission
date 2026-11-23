"""Replay the guarded second half of the one-minute boarding window against GPS."""
import argparse
from collections import Counter
import gzip
from hashlib import sha256
import json
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path, help="Full planner checkout with its prepared data and tools")
    parser.add_argument("extracted_gps", type=Path, help="Folder of extracted October bus_movements databases")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    project = args.project.resolve()
    sys.path.insert(0, str(project))
    from tools.replay_peak_router import CapturedOverlay, EtaModel, Router, Walking, matched_trip, prepared_rows, replay_one
    from tools.train_eta import open_db

    data = project / "data"
    model = EtaModel.load(data)
    router = Router(project / "delhi_buses_static_gtfs_v1.zip", data, Walking(data), model)
    registry = CapturedOverlay(model, data, 0).bind(router)
    report = {
        "scope": "31–60 s before platform readiness. Named forecast reconstructed from past GPS; "
                 "later GPS labels vehicle approach or exit. Rider boarding is unobserved.",
        "router_sha256": sha256((project / "transit/router.py").read_bytes()).hexdigest(), "dates": {},
    }
    for day, filename in [("2026-10-29", "near-miss-confirmation-opportunities.json.gz"),
                          ("2026-10-22", "near-miss-opportunities.json.gz"),
                          ("2026-10-26", "near-miss-opportunities.json.gz")]:
        with gzip.open(data / filename, "rt", encoding="utf-8") as f:
            selected = [r for r in json.load(f) if r["day"] == day and 31 <= r["gap"] <= 60]
        dbpath = args.extracted_gps / ("bus_movements_" + day.replace("-", "_") + ".db")
        with open_db(dbpath) as db:
            prepared = prepared_rows(db, selected)
        emitted, matched, reasons, evidence = Counter(), Counter(), Counter(), []
        for row, reason, candidate in prepared:
            entry = {"route": row["route"], "ready": row["ready"], "gap": row["gap"],
                     "outcome": row["outcome"], "stop": row["stop"], "run": row["run"]}
            if reason is None:
                clock, observations = candidate
                reason, trip = matched_trip(router, registry, row, clock)
                if trip:
                    actual = replay_one(router, registry, model, data, row, trip, observations, CapturedOverlay)
                    # The legacy replay helper reports acceptance on the ordinary
                    # 30-second departure API. The 31–60s secondary path uses
                    # boarding_departure(), so judge it by an actual emitted alert.
                    actual.pop("forecast_accepted_at_issue", None)
                    entry.update(actual)
                    matched[row["outcome"]] += 1
                    if actual["conditional_emitted"]:
                        emitted[row["outcome"]] += 1
            if reason:
                reasons[reason] += 1
                entry["excluded"] = reason
            evidence.append(entry)
        report["dates"][day] = {
            "opportunities": len(selected), "routes": len({r["route_id"] for r in selected}),
            "unconditional_60s": dict(Counter(r["outcome"] for r in selected)),
            "matched_to_active_trip": dict(matched),
            "guarded_60s_conditional_offers": dict(emitted), "unmatched": dict(reasons), "cases": evidence,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(day, len(selected), dict(emitted), flush=True)


if __name__ == "__main__":
    main()
