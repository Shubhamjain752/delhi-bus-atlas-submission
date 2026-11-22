"""Small repeatable offline HTTP concurrency check with real Delhi journey requests."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
import os
from pathlib import Path
import platform
from statistics import median
from time import perf_counter
from urllib.parse import urlencode, urlparse
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
PEAK = ("28.71815,77.06380","28.70700,77.17890")
QUERIES = [
    ("weekday_peak",*PEAK,"2026-10-15T08:45",None),
    ("weekday_midday",*PEAK,"2026-10-15T13:00",None),
    ("weekend_evening",*PEAK,"2026-10-24T18:30",None),
    ("pre_midnight","2383","2386","2026-10-23T23:55",None),
    ("post_midnight","2383","2386","2026-10-24T00:15",None),
    ("named_502","2383","2386","2026-11-21T01:18","nov21"),
]


def call(base,query):
    name,origin,destination,depart,replay = query
    params = {"from":origin,"to":destination,"depart":depart}
    if replay:
        params["replay"]=replay
    start=perf_counter()
    with urlopen(base+"/journeys?"+urlencode(params),timeout=180) as response:
        data=json.load(response)
    duration=perf_counter()-start
    stable={k:data.get(k) for k in ("journeys","walking_alternative","boarding_alerts","warnings")}
    if not stable["journeys"] and not stable["walking_alternative"]:
        raise AssertionError(f"Benchmark journey {name} produced no travel option")
    if name=="named_502" and not any(a.get("vehicle_approaching") and a.get("conditional_itinerary")
                                    for a in stable["boarding_alerts"]):
        raise AssertionError("Original after-midnight 502 case changed under load")
    digest=hashlib.sha256(json.dumps(stable,sort_keys=True).encode()).hexdigest()
    return name,duration,digest


def measure(base,repeat):
    if urlparse(base).hostname not in ("127.0.0.1","localhost","::1"):
        raise ValueError("This offline benchmark is for a local server")
    with urlopen(base+"/openapi.json",timeout=15) as response:
        version=json.load(response)["info"]["version"]
    with urlopen(base+"/status",timeout=30) as response:
        if json.load(response).get("live",{}).get("state") != "disabled":
            raise ValueError("Start the benchmark server with --offline")
    initial=[call(base,query) for query in QUERIES]
    expected={name:digest for name,_,digest in initial}
    batches=[]
    for clients in (1,4):
        start=perf_counter()
        with ThreadPoolExecutor(max_workers=clients) as pool:
            samples=list(pool.map(lambda query:call(base,query),QUERIES*repeat))
        elapsed=perf_counter()-start
        if any(expected[name]!=digest for name,_,digest in samples):
            raise AssertionError("Journey or boarding response changed between identical concurrent requests")
        times=sorted(seconds for _,seconds,_ in samples)
        by_query = {}
        for name in expected:
            durations = [seconds for sample_name, seconds, _ in samples if sample_name == name]
            by_query[name] = {"requests": len(durations), "median_seconds": round(median(durations), 3),
                              "maximum_seconds": round(max(durations), 3)}
        batches.append({"simultaneous_clients":clients,"queries":len(times),"total_seconds":round(elapsed,3),
                        "requests_per_second":round(len(times)/elapsed,3),"median_seconds":round(median(times),3),
                        "p95_seconds":round(times[math.ceil(.95*len(times))-1],3),"maximum_seconds":round(max(times),3),
                        "by_query":by_query})
    return {"api_version":version,"server_mode":"single local offline process","python":platform.python_version(),
            "platform":platform.system(),"logical_cores":os.cpu_count(),"initial_queries_excluded":len(QUERIES),
            "queries":[q[0] for q in QUERIES],"batches":batches,
            "interpretation":"A closed-loop warm comparison with six repeated OD/time requests on one machine. "
                             "Live ingestion, cold starts and city passenger demand require separate measurement."}


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base",default="http://127.0.0.1:8766")
    parser.add_argument("--repeat",type=int,default=3)
    parser.add_argument("--output",type=Path)
    args=parser.parse_args()
    if args.repeat<1:
        parser.error("--repeat must be positive")
    result=json.dumps(measure(args.base.rstrip("/"),args.repeat),indent=2)
    if args.output:
        args.output.write_text(result+"\n",encoding="utf-8")
    print(result)
