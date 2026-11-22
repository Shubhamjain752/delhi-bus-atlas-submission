"""Replay registered November route choices on all four matching October weekdays."""
import argparse
from datetime import date, datetime
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tools.month_boarding_scan import PREVIOUS_EXTRACTS, archive_members, daily_database
from tools.score_simulated_rides import fingerprint, run


def run_month(study, archive, seven):
    members = archive_members(seven, archive)
    scenarios = []
    for path in sorted(study.glob("*/simulation/*-scenario.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("case") not in {"sunday-offpeak","monday-peak","tuesday-transfer"}:
            continue
        scenarios.append((path, raw))
    if len(scenarios)!=6:
        raise ValueError("Expected both frozen route choices for each of the three queries")
    counts = {}
    for day,member in sorted(members.items()):
        weekday = date.fromisoformat(day).weekday()
        matches = [(path,s) for path,s in scenarios if datetime.fromisoformat(s["departure"]).weekday()==weekday]
        if not matches:
            continue
        with daily_database(seven,archive,member,PREVIOUS_EXTRACTS,Path(tempfile.gettempdir())) as (db,_):
            for path, original in matches:
                scenario = {k:v for k,v in original.items() if not k.startswith("forecast")}
                scenario["original_departure"] = original["departure"]
                scenario["departure"] = day+original["departure"][10:]
                scenario["test_type"] = "historical_route_analogue_without_a_prospective_forecast"
                obj = run(scenario,db,120,member)
                obj["scenario_file_sha256"] = fingerprint(path)
                out = path.with_name(original["arm"]+"-analogue-"+day+".json")
                tmp = out.with_suffix(".pending")
                tmp.write_text(json.dumps(obj,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
                tmp.replace(out)
                strict = obj["simulations"][0]["status"]
                relaxed = obj["supplemental_directed_shape_crossings"]["simulations"][0]["status"]
                counts.setdefault(day,{})[original["arm"]] = {"case":original["case"],"strict":strict,"shape_crossing":relaxed}
                print(day,original["arm"],strict,relaxed,flush=True)
    if len(counts)!=12 or any(len(group)!=2 for group in counts.values()):
        raise ValueError("Some route/date trials are missing")
    return counts


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study",type=Path,default=ROOT/"data/whole-journey")
    parser.add_argument("--archive",type=Path,default=ROOT/"delhi-dtc-bus-gtfs-rt-2026-10.7z")
    parser.add_argument("--seven",type=Path,default=Path("C:/Program Files/7-Zip/7z.exe"))
    args=parser.parse_args()
    result=run_month(args.study,args.archive,args.seven)
    print("24 fixed-itinerary date comparisons complete.")
