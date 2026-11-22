"""Summarize the fixed date sample, including censored vehicles."""
from collections import Counter
import json
from pathlib import Path
from statistics import median
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tools.score_simulated_rides import epoch, fingerprint, ride_case


def first_reported_bus(data, method):
    """Separate boarding assumption. Never discard an earlier bus using its later path."""
    legs=[]
    for candidates in method["gps_passages_by_leg"]:
        rows=[]
        for original in candidates:
            row=dict(original)
            # The rider takes the first recorded bus carrying this route/direction.
            # The target must still be seen on that very vehicle before a total exists.
            row["pattern_status"]="serves"
            for field in ("board","alight"):
                if row[field] is not None:
                    row[field]={k:epoch(v) if k in ("lower","upper","received") else v for k,v in row[field].items()}
            rows.append(row)
        legs.append(rows)
    distances = data["walk_distance_m_access_transfers_egress"]
    flags = data["walk_graph_flags"]
    stated = data["scenario"].get("published_walk_metres")
    if stated is not None:
        distances = [published if flag else measured for flag,published,measured in zip(flags,stated,distances,strict=True)]
        flags = [None]*len(distances)
    return ride_case(data["scenario"],legs,distances,1.25,0,method["route_ids_with_compatible_geometry_by_leg"],flags)


def summarize(study):
    result = {}
    for name in ("sunday-offpeak","monday-peak","tuesday-transfer"):
        for provider in ("atlas","google"):
            folder = study/name/"simulation"
            inputs = sorted(folder.glob(provider+"-analogue-*.json"))
            if len(inputs)!=4:
                raise ValueError(f"Incomplete October weekday sample: {name} / {provider}")
            cases=[]
            scenario_hash = fingerprint(folder/(provider+"-scenario.json"))
            for path in inputs:
                with path.open(encoding="utf-8") as f:
                    data=json.load(f)
                if data.get("scenario_file_sha256")!=scenario_hash:
                    raise ValueError("Historical result predates the locked scenario: "+str(path))
                cases.append(data)
            methods={}
            for method in ("strict","shape_crossings"):
                counts=Counter()
                censored=Counter()
                assisted=Counter()
                exposures=Counter()
                legs={}
                examples=[]
                for data in cases:
                    section = data if method=="strict" else data["supplemental_directed_shape_crossings"]
                    decision=section["simulations"][0]
                    censored[decision["status"]]+=1
                    if "with_published_distances_for_unmapped_walks" in section:
                        assisted[section["with_published_distances_for_unmapped_walks"]["simulations"][0]["status"]]+=1
                    seen = decision["events"]
                    date = data["scenario"]["departure"][:10]
                    for i, (leg, traces) in enumerate(zip(data["scenario"]["legs"],section["gps_passages_by_leg"])):
                        key = str(i+1)
                        bag = legs.setdefault(key,{})
                        completed = {}
                        for v in traces:
                            if v["pattern_status"] == "serves" and v["alight"] is not None:
                                # Count the first entirely observed passage of a physical run
                                # on this date and leg. A GPS jitter episode cannot add riders.
                                completed.setdefault(v["run_token"],v)
                        for tk,row in completed.items():
                            start = row["board"]
                            end = row["alight"]
                            bag[date,tk] = (epoch(end["lower"])+epoch(end["upper"])-epoch(start["lower"])-epoch(start["upper"]))/2
                    for kind in ("board","alight"):
                        counts[kind+"_proxy_events"] += sum(e["kind"]==kind for e in seen)
                    if any(e["kind"]=="board" for e in seen):
                        counts["dates_with_first_board_proxy"]+=1
                    if any(e["kind"]=="alight" for e in seen):
                        counts["dates_with_first_alight_proxy"]+=1
                    if sum(e["kind"]=="board" for e in seen)>1:
                        counts["dates_with_second_board_proxy"]+=1
                    for before,after in zip(seen,seen[1:]):
                        if after["kind"]=="board":
                            exposures[date,after["route"]] = epoch(after["at"])-epoch(before["at"])
                    examples.append({"date":date,"status":decision["status"],"events":[[e["kind"],e["at"]] for e in seen]})
                conditional=[]
                for data in cases:
                    section = data if method=="strict" else data["supplemental_directed_shape_crossings"]
                    outcome=first_reported_bus(data,section)
                    conditional.append({"date":data["scenario"]["departure"][:10],**outcome})
                methods[method]={"virtual_rider_on_four_dates":dict(counts),"outcomes":dict(censored),
                                 "with_provider_walk_distance_outcomes":dict(assisted),
                                 "posthoc_first_reported_route_number_bus":conditional,
                                 "waits_until_witnessed_boardings_seconds":[{"date":key[0],"route":key[1],"seconds":n} for key,n in exposures.items()],
                                 "per_leg_independent_complete_gps_runs":{k:{"runs":len(v),"median_seconds":median(v.values()) if v else None}
                                                                           for k,v in legs.items()},
                                 "partial_outcomes":examples}
            result[name+"_"+provider]={"dates":[d["scenario"]["departure"][:10] for d in cases],"methods":methods}
    return {"cases":result,"case_date_arms":24,"interpretation":"Physical bus passages with simulated walking and waiting. Per-leg counts include separate runs throughout the window, including before departure. No November forecast is scored on October traffic."}


if __name__=="__main__":
    study=ROOT/"data/whole-journey"
    report=summarize(study)
    out=study/"historical-simulation-summary.json"
    out.write_text(json.dumps(report,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
    print(out)
