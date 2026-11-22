"""Record the finite prospective GPS windows in the published Delhi case plan."""
import argparse
from datetime import datetime, timedelta, timezone
import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from transit.live import PUBLIC_TRIPS, download

IST = timezone(timedelta(hours=5, minutes=30))
STUDY = ROOT / "data" / "whole-journey"
RAW = ROOT / ".whole-journey-witness"
URL = PUBLIC_TRIPS.replace("trip_updates", "vehicle_positions")


def save(path, value):
    pending = path.with_name(path.name + ".tmp")
    pending.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    pending.replace(path)


def wait_until(instant):
    while True:
        remaining = instant - time.time()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 30))


def windows(cases, wanted):
    if len(wanted) != len(set(wanted)) or not wanted:
        raise ValueError("Choose distinct registered cases")
    for name in sorted(wanted, key=lambda k: cases[k]["departure"]):
        depart = datetime.fromisoformat(cases[name]["departure"])
        yield name, depart, depart - timedelta(minutes=30), depart + timedelta(minutes=125)


def record(name, departure, start, end):
    directory = RAW / departure.date().isoformat()
    directory.mkdir(parents=True, exist_ok=True)
    status_path = STUDY / name / "witness-status.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    if status_path.is_file() and json.loads(status_path.read_text()).get("state") == "scored":
        return
    status = {"case":name,"scheduled_start":start.isoformat(),"scheduled_end":end.isoformat(),
              "departure":departure.isoformat(),"source":URL,"pid":os.getpid(),"state":"waiting",
              "updated_at":datetime.now(IST).isoformat(timespec="seconds")}
    save(status_path,status)
    wait_until(start.timestamp())
    near_departure = None
    next_attempt = 0
    while time.time() < end.timestamp():
        began = time.time()
        near_status = STUDY / "near-departure" / name / "status.json"
        if (departure.timestamp() - 600 <= began < departure.timestamp() - 100
                and (near_departure is None or near_departure.poll() not in (None, 0))
                and began >= next_attempt
                and (not near_status.exists() or json.loads(near_status.read_text(encoding="utf-8")).get("state") != "complete")):
            near_departure = subprocess.Popen([sys.executable, "-u", str(ROOT / "tools" / "capture_near_departure.py"), name], cwd=ROOT)
            next_attempt = began + 90
        try:
            feed = download(URL)
            receipt = int(time.time())
            file = directory / f"positions-{receipt}-{feed.header.timestamp}.pb.gz"
            if not file.exists():
                temporary = file.with_suffix(".tmp")
                temporary.write_bytes(gzip.compress(feed.SerializeToString(), compresslevel=3, mtime=0))
                temporary.replace(file)
            status.update(state="recording", last_received=receipt, feed_header=feed.header.timestamp)
            status.pop("last_error",None)
        except Exception as error:
            status.update(state="recording_with_errors",last_error=f"{type(error).__name__}: {error}")
        status["updated_at"]=datetime.now(IST).isoformat(timespec="seconds")
        save(status_path,status)
        wait_until(min(end.timestamp(),began+30))
    status.update(state="scoring", position_files=sum(1 for _ in directory.glob("positions-*.pb.gz")))
    save(status_path,status)
    outputs={}
    simulation=STUDY / name / "simulation"
    for provider in ("atlas","google"):
        scenario=simulation / (provider+"-scenario.json")
        output=simulation / (provider+"-result.json")
        if not scenario.is_file():
            outputs[provider] = "no verified route and stop crosswalk"
            continue
        if not status["position_files"]:
            outputs[provider] = "no archived position snapshots"
            continue
        process=subprocess.run([sys.executable,str(ROOT/"tools"/"score_dated_rides.py"),str(scenario),
                                str(directory),str(output),"--horizon-minutes","120"],cwd=ROOT)
        outputs[provider] = "scored" if process.returncode==0 and output.exists() else "scorer failed"
    status.update(state="scored" if all(v=="scored" for v in outputs.values()) else "ended_with_gaps",
                  outcomes=outputs,updated_at=datetime.now(IST).isoformat(timespec="seconds"))
    save(status_path,status)
    print(name,status["state"],outputs,flush=True)
    packed = subprocess.run([sys.executable, str(ROOT / "tools" / "export_field_evidence.py")], cwd=ROOT)
    if packed.returncode:
        print(name, "dated evidence is on disk; ZIP export failed", flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case",nargs="+")
    parser.add_argument("--show",action="store_true")
    args=parser.parse_args()
    plan=json.loads((STUDY/"plan.json").read_text())["cases"]
    for name,depart,start,end in windows(plan,args.case):
        print(name,start.isoformat(),end.isoformat(),flush=True)
        if not args.show:
            record(name,depart,start,end)


if __name__=="__main__":
    main()

