"""Count bare and suffixed provider clocks in six preselected development windows."""
import argparse
from collections import Counter,defaultdict
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
from contextlib import closing

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tools.train_eta import boundary, open_db
from transit.live import LiveOverlay
from transit.schedule import cache_schedule

WINDOWS = [('00:59:00','01:01:00'),('08:29:00','08:31:00'),('18:29:00','18:31:00')]
DAYS = ['2026-10-22','2026-10-23']


def registry():
    clocks = Counter()
    cache = cache_schedule(ROOT/'delhi_buses_static_gtfs_v1.zip',ROOT/'data')
    with closing(sqlite3.connect(cache)) as cx:
        for route,departures,trips in cx.execute('select route,departures,trips from patterns'):
            dwell = json.loads(departures)[0]
            for start,_ in json.loads(trips):
                clocks[route,(start+dwell)//60]+=1
    return clocks


def audit(directory):
    clocks = registry()
    windows = []
    totals = Counter()
    lengths = Counter()
    for day in DAYS:
        path = directory/('bus_movements_'+day.replace('-','_')+'.db')
        with open_db(path) as db:
            for start,end in WINDOWS:
                lo,hi = boundary(db,start),boundary(db,end)
                claims = defaultdict(set)
                counts = Counter()
                for rt,run,vehicle,receipt in db.execute(
                        'SELECT route_id,trip_id,vehicle_id,time FROM vehicle_feed WHERE rowid>=? AND rowid<?',(lo,hi)):
                    counts['broadcasts']+=1
                    if rt is None or run is None:
                        continue
                    route,raw = str(int(rt)),str(run)
                    minute = LiveOverlay.provider_clock(route,raw)
                    if minute is None:
                        continue
                    counts['parsed_now']+=1
                    bare = bool(re.fullmatch(re.escape(route)+r'_[0-9]{1,2}_[0-5][0-9]',raw))
                    if bare:
                        counts['parsed_previously']+=1
                    else:
                        lengths[len(raw.rsplit('_',1)[1])]+=1
                    n = clocks[route,minute]
                    if n==1:
                        counts['one_timetable_trip_at_clock_now']+=1
                        counts['one_timetable_trip_at_clock_previously']+=int(bare)
                    elif n>1:
                        counts['multiple_timetable_trips_at_clock']+=1
                    else:
                        counts['no_timetable_trip_at_clock']+=1
                    if vehicle is not None:
                        claims[receipt,route,minute].add(str(vehicle))
                counts['observed_receipt_route_clock_groups'] = len(claims)
                counts['groups_with_multiple_physical_claimants'] = sum(len(v)>1 for v in claims.values())
                if counts['broadcasts']!=hi-lo:
                    raise ValueError('Raw rows and receipt-window bounds differ')
                windows.append({'day':day,'start_ist':start,'end_ist_exclusive':end,**dict(counts)})
                totals.update(counts)
    return {'protocol':'delhi-provider-clock-format-audit-v1','development_days':DAYS,'windows':windows,
            'totals':dict(totals),'suffix_digit_lengths':dict(sorted(lengths.items())),
            'input_archive_sha256':(ROOT/'SHA256SUMS.txt').read_text().split()[0],
            'evaluator_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--existing',type=Path,default=ROOT/'.near-miss-peak-20261121')
    p.add_argument('--output',type=Path,default=ROOT/'data/fleet-provider-clocks.json')
    args=p.parse_args()
    payload=audit(args.existing)
    temp = args.output.with_suffix('.pending')
    temp.write_text(json.dumps(payload,indent=2),encoding='utf-8')
    temp.replace(args.output)
    print(json.dumps(payload['totals']))
