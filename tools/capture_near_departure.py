"""Capture a separate pair of forecasts ten minutes before a registered journey."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlencode, urlparse
from urllib.request import urlopen

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools.whole_journey_study import PROJECT, STUDY, as_time, classify, normalize_atlas, read, seconds, verify_lock

IST = timezone(timedelta(hours=5, minutes=30))
CLOCK = re.compile(r"(\d{1,2}):(\d{2})\s*([AP]M)", re.I)


def now():
    return datetime.now(IST)


def save(path, value):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, encoding="utf-8", delete=False) as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        name = f.name
    os.replace(name, path)


def remaining(case):
    return (as_time(case["departure"]) - now()).total_seconds()


def check_capture(case, recorded):
    if not 0 < seconds(case["departure"], recorded) <= 720:
        raise ValueError("The forecast was not received during the twelve minutes before departure")


def atlas(key, case, folder, base):
    if urlparse(base).hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("Atlas capture requires its local server")
    origin, dest = (",".join(map(str, case[n])) for n in ("origin", "destination"))
    params = {"from":origin, "to":dest, "depart":case["departure"], "limit":5}
    url = base.rstrip("/") + "/journeys?" + urlencode(params)
    started = now().isoformat()
    with urlopen(url,timeout=max(1,min(90,remaining(case)))) as resp:
        raw = resp.read()
    captured = now().isoformat()
    check_capture(case,captured)
    (folder/"atlas.json").write_bytes(raw)
    path = normalize_atlas(json.loads(raw))
    choice = classify(case,path)["earliest"]
    with urlopen(base.rstrip("/")+"/openapi.json",timeout=10) as resp:
        version = json.load(resp)["info"]["version"]
    timing = {"requested_at":started, "captured_at":captured, "url":url, "api_version":version}
    save(folder/"atlas-timing.json",timing)
    return timing, (choice["arrival"] if choice else None), ([leg["route"] for leg in choice["legs"]] if choice else [])


def google(key, case, folder, _base):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    paths = [str(PROJECT/package) for package in (".runtime", ".runtime-dev") if (PROJECT/package).is_dir()]
    env["PYTHONPATH"] = os.pathsep.join(paths + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    with tempfile.TemporaryDirectory(prefix="google-public-transit-") as stage:
        cmd = [sys.executable, str(PROJECT/"tools/capture_google_transit.py"), key, "--output", stage]
        source = Path(stage) / key / "capture"
        try:
            completed = subprocess.run(cmd, cwd=PROJECT, env=env, capture_output=True,
                                       encoding="utf-8",errors="replace",timeout=max(1,min(120,remaining(case))))
        finally:
            for name in ("google.txt","google-options.png","google.png","google-timing.json"):
                if (source/name).is_file():
                    shutil.copyfile(source/name,folder/name)
        if completed.returncode:
            raise ValueError((completed.stderr or completed.stdout)[-1700:])
        timing = read(source/"google-timing.json")
        check_capture(case,timing["captured_at"])
        if any(timing[n] != case[n] for n in ("origin","destination","departure")) or not timing["bus_preferred"]:
            raise ValueError("Mismatched Google query")
        header = timing["chosen_option"].splitlines()[0]
        clocks = CLOCK.findall(header)
        buses = re.findall(r"\bBus ([A-Za-z0-9][A-Za-z0-9()/-]*)",header)
        if len(clocks)<2 or not buses:
            raise ValueError("Could not read the chosen bus and destination clock")
        h,m,period = clocks[1]
        departure = as_time(case["departure"])
        arrival = departure.replace(hour=int(h)%12+(12 if period.upper()=="PM" else 0),
                                    minute=int(m),second=0,microsecond=0)
        if arrival<departure:
            arrival += timedelta(days=1)
    return timing, arrival.isoformat(), buses


def collect(key, case, provider, destination, base):
    old = read(STUDY/key/provider/"prediction.json")["earliest"]
    with tempfile.TemporaryDirectory(prefix=f".{provider}-",dir=destination) as stage:
        folder = Path(stage)
        try:
            timing,arrival,buses = {"atlas":atlas,"google":google}[provider](key,case,folder,base)
            result = {"state":"captured" if arrival else "no_bus_estimate", "arrival":arrival,
                      "routes":buses, "requested_at":timing["requested_at"], "captured_at":timing["captured_at"],
                      "lead_seconds":seconds(case["departure"],timing["captured_at"]),
                      "duration_seconds":seconds(arrival,case["departure"]) if arrival else None,
                      "primary_arrival":old["arrival"] if old else None,
                      "arrival_change_seconds":seconds(arrival,old["arrival"]) if old and arrival else None,
                      "primary_routes":[leg["route"] for leg in old["legs"]] if old else [],
                      "raw_sha256":{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir()}}
            for evidence in folder.iterdir():
                evidence.replace(destination/evidence.name)
            return result
        except Exception as error:
            failure = {"state":"failed", "at":now().isoformat(), "reason":str(error)}
            if any(folder.iterdir()):
                evidence_dir = destination.parent/"failed-attempts"/folder.name.lstrip(".")
                shutil.copytree(folder,evidence_dir)
                failure["saved_files"] = sorted(str((evidence_dir/p.name).relative_to(destination.parent))
                                                for p in folder.iterdir())
            return failure


def execute(key, base, wait=False):
    verify_lock(STUDY)
    case = read(STUDY/"plan.json")["cases"][key]
    folder = STUDY/"near-departure"/key
    folder.mkdir(parents=True,exist_ok=True)
    capture = folder/"capture"
    capture.mkdir(exist_ok=True)
    status = folder/"status.json"
    with closing(sqlite3.connect(folder/".capture-lock.sqlite",timeout=0)) as lock:
        lock.execute("BEGIN IMMEDIATE")
        result = read(status) if status.exists() else {
            "case":key, "query":case, "kind":"separate_near_departure_forecast",
            "target_lead_seconds":600, "providers":{}}
        if result["query"] != case:
            raise ValueError("Preregistered query changed")
        pending = [p for p in ("atlas","google") if result["providers"].get(p,{}).get("state")
                   not in ("captured","no_bus_estimate")]
        if not pending:
            return result
        if remaining(case)>720 or (wait and remaining(case)>600):
            result["state"] = "pending"
            save(status,result)
            if not wait:
                return result
            print("Waiting for the registered ten-minute lead window.",flush=True)
            while remaining(case)>600:
                time.sleep(max(0,min(remaining(case)-600,55)))
        if remaining(case)<=0:
            result["state"] = "incomplete_after_departure"
            save(status,result)
            return result
        with ThreadPoolExecutor(max_workers=len(pending)) as pool:
            jobs = {p:pool.submit(collect,key,case,p,capture,base) for p in pending}
            for p,job in jobs.items():
                prediction = job.result()
                if prediction["state"] == "failed":
                    result.setdefault("attempt_errors",[]).append({"provider":p,**prediction})
                result["providers"][p]=prediction
                save(status,result)
        result["state"] = ("complete" if all(p["state"] in ("captured","no_bus_estimate")
                                             for p in result["providers"].values()) else "partial_or_failed")
        if all(r.get("captured_at") for r in result["providers"].values()):
            a,b = result["providers"].values()
            result["pair_capture_skew_seconds"] = abs(seconds(a["captured_at"],b["captured_at"]))
        verify_lock(STUDY)
        save(status,result)
        return result


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", choices=read(STUDY/"plan.json")["cases"])
    parser.add_argument("--base", default="http://127.0.0.1:8765")
    parser.add_argument("--wait",action="store_true", help="Wait until ten minutes before departure")
    args=parser.parse_args()
    result=execute(args.case,args.base,args.wait)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    if result["state"] not in ("complete","pending"):
        raise SystemExit(1)
