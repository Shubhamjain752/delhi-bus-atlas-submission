"""Build the all-route all-hour Delhi GPS near-miss cohort from private hourly shards."""
import argparse
from collections import Counter
from datetime import date, datetime, time, timedelta
import hashlib
import inspect
import json
from pathlib import Path
import sys
import time as timer

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tools.fleet_gps import FIX, MISSING_ROUTE
from tools.fleet_screen import geometry, independent_label, METRICS, screen
from tools.month_boarding_scan import MONTH, save
from tools.near_miss_peak import token
from tools.replay_fleet_boarding import TimetableMatcher
from transit.live import TTL, LiveOverlay
from transit.router import Router, IST

SCHEMA = 1
PROTOCOL = 'delhi-all-routes-all-hours-v1'
CELL_FIELDS = ('raw','valid',*METRICS)
LABELS = ('approaching','gone','uncertain','unverified')


def epoch(day):
    return int(datetime.combine(date.fromisoformat(day),time(),IST).timestamp())


def neighbor(day, h):
    value = datetime.combine(date.fromisoformat(day),time()) + timedelta(hours=h)
    return value.date().isoformat(),value.hour


def shard(directory,d,h):
    return directory/f'{d}-{h:02d}.npy'


def available(directory, d):
    if not (directory/f'{d}.json').is_file():
        return False
    # A window may own a readiness just after midnight using a previous-day anchor.
    # A label may require fixes in the next day's first minutes.
    return all(when not in MONTH or shard(directory,when,h).is_file()
               for when,h in (neighbor(d,-1),neighbor(d,24)))


def window(directory,d,h,core):
    parts = []
    for offset in (-1,0,1):
        day,hour = neighbor(d,h+offset)
        if day not in MONTH:
            continue
        a = np.load(shard(directory,day,hour),mmap_mode='r',allow_pickle=False)
        # 600s upstream history + maximum 240s runtime + 60s readiness offset;
        # observations can arrive up to 90s late. 18 minutes covers all bounds.
        if offset<0:
            a = a[np.searchsorted(a['recv'],core-1080):]
        elif offset>0:
            a = a[:np.searchsorted(a['recv'],core+3600+1080)]
        parts.append(a)
    return np.concatenate(parts) if parts else np.empty(0,FIX)


def evaluate_hour(directory,d,h,census,g,matcher):
    core = epoch(d)+h*3600
    gps = window(directory,d,h,core)
    # Stable sort preserves receipt order within a physical bus.
    bybus = np.argsort(gps['bus'],kind='stable')
    a = gps[bybus]
    metrics, candidates, at, broken = screen(a,core,*g.arrays)
    reported = {int(r) if r!='missing' else MISSING_ROUTE for r in census}
    cells = {}
    for r in sorted(reported.union(np.flatnonzero(metrics.any(axis=1)))):
        name = str(r) if r!=MISSING_ROUTE else 'missing'
        cells[name] = [*census.get(name,[0,0]),*map(int,metrics[r])]
    if not candidates:
        return cells,[]
    # Route + provider-clock lookups independently detect different active
    # physical claimants. All comparisons below have an explicit receipt cutoff.
    clocks = gps['route'].astype('i8')*10000 + np.where(gps['minute']<0,9999,gps['minute']%1440)
    order = np.argsort(clocks,kind='stable')
    sort_clock, sort_recv = clocks[order],gps['recv'][order]
    def identity_at(i, when, collect=False):
        f = a[i]
        r = int(f['route'])
        b,t,m = int(f['bus']),int(f['trip']),int(f['minute'])
        first,last = np.searchsorted(a['bus'],np.uint64(b),side='left'),np.searchsorted(a['bus'],np.uint64(b),side='right')
        receipts = a['recv'][first:last]
        lo = first+np.searchsorted(receipts,when-TTL)
        hi = first + np.searchsorted(receipts,when,side='right')
        seen = a[lo:hi]
        seen = seen[seen['obs']>=when-TTL]
        named = (seen['route']==r)&(seen['trip']==np.uint64(t))
        reassigned = bool(np.any(~named & (seen['obs']>=f['obs'])))
        observations = []
        if collect:
            for q in seen[named]:
                observations.append([int(q['recv']),int(q['obs']),float(q['lat']),float(q['lon'])])
        reason = None
        if m<0:
            reason = 'unrecognized_trip_clock'
        else:
            key = int(r)*10000 + m%1440
            start = np.searchsorted(sort_clock,key)
            end = np.searchsorted(sort_clock,key,side='right')
            group = sort_recv[start:end]
            startq = start + np.searchsorted(group,when-TTL)
            endq = start + np.searchsorted(group,when,side='right')
            claim = gps[order[startq:endq]]
            if reassigned:
                reason = 'vehicle_reassigned_before_issue'
            elif np.any((claim['obs']>=when-TTL)&(claim['bus']!=np.uint64(b))):
                reason = 'multiple_recent_vehicles_claim_clock'
        return reason,observations

    # Within each route-hour, try every candidate in readiness order until the
    # real policy's calendar matcher and the issue-time identity checks agree.
    # Preserve the first unfiltered reason and inspect later GPS only after selection.
    chosen,first_reason,checked = {},{},Counter()
    matched = set()
    for when,i,j in sorted(candidates):
        f = a[i]
        r,m = int(f['route']),int(f['minute'])
        if r in matched:
            continue
        checked[r] += 1
        row = {'day':d,'route_id':str(r),'start_minute':m if m>=0 else None,
               'upstream':g.names[j],'stop':g.names[j+1],'next_stop':g.names[j+2],
               'forecast':int(f['obs'])+int(g.arrays[3][j])}
        static_reason,_,_ = matcher.match(row)
        reason = static_reason
        if not static_reason or r not in chosen:
            identity_reason,_ = identity_at(i,when)
            reason = identity_reason or static_reason
        if r not in chosen:
            chosen[r] = (when,i,j)
            first_reason[r] = reason
        if not reason:
            chosen[r] = (when,i,j)
            matched.add(r)
    cases = []
    for r,(when,i,j) in sorted(chosen.items()):
        f = a[i]
        b,t,m = int(f['bus']),int(f['trip']),int(f['minute'])
        reason,observations = identity_at(i,when,collect=True)
        label,prior,entered,left = independent_label(a,i,j,when,at,broken,g.arrays[1],g.arrays[2])
        bracket = [int(a[k]['obs']) if k>=0 else None for k in [prior,entered,left]] if entered>=0 else None
        received = [int(a[k]['recv']) if k>=0 else None for k in [prior,entered,left]] if entered>=0 else None
        forecast = int(f['obs'])+int(g.arrays[3][j])
        count = g.pattern_counts.get(int(r)*10000+m,0) if m>=0 else 0
        pattern = 'unique_clock_pattern' if count==1 else ('clock_pattern_tie' if count else 'route_representative')
        cases.append({'day':d,'hour':h,'route_id':str(r),'upstream':g.names[j],'stop':g.names[j+1],
                      'next_stop':g.names[j+2],'bus':token(b,'fleet-bus'),'run':token(d,b,t,'fleet-run'),
                      'start_minute':m if m>=0 else None,'origin_gps':int(f['obs']),'forecast_receipt':int(f['recv']),
                      'ready':when,'forecast':forecast,'gap':when-forecast,'identity_reason':reason,
                      'geometry_source':pattern,'observations':observations,'outcome':LABELS[label],
                      'first_candidate_exclusion':first_reason[r], 'candidates_checked':checked[r],
                      'eligible_named_case':r in matched,
                      'gps_entry_exit':bracket,'gps_entry_exit_receipts':received})
    return cells,cases


def manifest(router):
    files = ['tools/fleet_gps.py','tools/fleet_hourly.py','tools/fleet_screen.py','tools/fleet_cases.py',
             'tools/near_miss_peak.py','tools/train_eta.py','tools/month_boarding_scan.py','delhi_buses_static_gtfs_v1.zip']
    vals = {name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in files}
    return {'protocol':PROTOCOL,'schema':SCHEMA,'source_sha256':vals,
            'matcher_sha256':hashlib.sha256(inspect.getsource(TimetableMatcher).encode()).hexdigest(),
            'source_archive_sha256':(ROOT/'SHA256SUMS.txt').read_text().split()[0],
            'cell_fields':CELL_FIELDS,'route_names':{r:n[0] for r,n in router.routes.items()},
            'development_days':[d for d in MONTH if d<'2026-10-24'],
            'confirmation_days':[d for d in MONTH if d>='2026-10-24']}


def write_day(directory,d,g,matcher,out,signature):
    census = json.loads((directory/f'{d}.json').read_text())
    result = {'schema':SCHEMA,'protocol':PROTOCOL,'signature':signature,'day':d,'crc32':census['crc32'],
              'cell_fields':CELL_FIELDS,'hours':[],'cases':[],'complete':False}
    for hour,counts in enumerate(census['hours']):
        cells,cases = evaluate_hour(directory,d,hour,counts,g,matcher)
        result['hours'].append(cells)
        result['cases'].extend(cases)
    assert len({(c['hour'],c['route_id']) for c in result['cases']})==len(result['cases'])
    result['complete'] = True
    save(out/f'{d}.json.gz',result,compressed=True)
    print(d,'published',len(result['cases']),'route-hour GPS cases',flush=True)


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scratch',type=Path,required=True)
    parser.add_argument('--output',type=Path,default=ROOT/'data'/'fleet-cases')
    parser.add_argument('--days',nargs='+',choices=MONTH,default=MONTH)
    parser.add_argument('--wait',action='store_true',help='Process dates as their temporary neighboring shards become available.')
    args = parser.parse_args()
    r = Router(ROOT/'delhi_buses_static_gtfs_v1.zip', ROOT/'data',None,None)
    g = geometry(r)
    matcher = TimetableMatcher(r,LiveOverlay(None,ROOT/'data').bind(r))
    protocol = manifest(r)
    signature = hashlib.sha256(json.dumps(protocol,sort_keys=True).encode()).hexdigest()
    args.output.mkdir(parents=True,exist_ok=True)
    path = args.output/'manifest.json'
    if path.exists() and json.loads(path.read_text())['signature'] !=signature:
        raise ValueError('Existing fleet output is from another protocol or code revision.')
    if not path.exists():
        save(path,dict(protocol,signature=signature))
    remaining = [d for d in args.days if not (args.output/f'{d}.json.gz').is_file()]
    while remaining:
        pending = [d for d in remaining if available(args.scratch,d)]
        if not pending:
            if not args.wait:
                raise ValueError('Missing complete temporary shards for '+','.join(remaining))
            timer.sleep(10)
            continue
        for d in pending:
            write_day(args.scratch,d,g,matcher,args.output,signature)
            remaining.remove(d)
