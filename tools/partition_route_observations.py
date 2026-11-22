"""Publish platform-movement counts on the actual GPS receipt date in Delhi."""
import argparse
from collections import Counter,defaultdict
from datetime import datetime
import gzip
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tools.fleet_anonymous_arrivals import Audit,CONFIRM,DEV,MONTH,aggregate,cases,save
from transit.router import IST


def issue_day(stamp):
    return datetime.fromtimestamp(stamp,IST).date().isoformat()


def unresolved(audit,row):
    minute=row.get('start_minute')
    return minute is not None and int(row['route_id'])*10000+minute not in audit.published


def empty_day():
    return {'coverage':Counter(),'rows':[]}


def compact(row):
    return json.dumps(row,sort_keys=True,separators=(',',':'))


def move_counts(staged,ready,issue,reason):
    counts=Counter(route_hour_candidates=1)
    if reason:
        counts.update(['unresolved_clock',reason])
    staged[ready]['coverage'].subtract(counts)
    staged[issue]['coverage'].update(counts)


def publish(case_dir,output):
    audit=Audit()
    evaluator=hashlib.sha256((ROOT/'tools/fleet_anonymous_arrivals.py').read_bytes()).hexdigest()
    policy=hashlib.sha256((ROOT/'transit/route_observation.py').read_bytes()).hexdigest()
    partitioner=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    staged=defaultdict(empty_day)
    observed=defaultdict(Counter)
    routes=defaultdict(set)
    unresolved_routes=defaultdict(set)
    ranges={}
    crossing=[]
    expected=Counter()
    for day in MONTH:
        source=case_dir/(day+'.json.gz')
        with gzip.open(output/(day+'.json.gz'),'rt',encoding='utf-8') as stream:
            data=json.load(stream)
        if (data['script_sha256']!=evaluator or data['policy_sha256']!=policy or
                data['source_sha256']!=hashlib.sha256(source.read_bytes()).hexdigest() or data['day']!=day):
            raise ValueError('Source, evaluator, policy or calendar mismatch at '+day)
        staged[day]['coverage'].update(data['coverage'])
        for hint in data['rows']:
            issued=issue_day(hint['issue'])
            staged[issued]['rows'].append(hint)
            if issued!=day:
                expected[compact(hint)]+=1
        for row in cases(source):
            stamp=row['forecast_receipt']
            issued=issue_day(stamp)
            if row['day']!=day or issue_day(row['ready'])!=day or stamp>row['ready']:
                raise ValueError('Invalid source-date attribution at '+day)
            observed[issued]['route_hour_candidates']+=1
            routes[issued].add(row['route_id'])
            low,high=ranges.get(issued,(stamp,stamp))
            ranges[issued]=min(low,stamp),max(high,stamp)
            if unresolved(audit,row):
                observed[issued]['unresolved_clock']+=1
                unresolved_routes[issued].add(row['route_id'])
            if issued!=day:
                crossing.append((row,issued))

    replayed=Counter()
    flow=defaultdict(Counter)
    route=None
    for row,issued in sorted(crossing,key=lambda item:item[0]['route_id']):
        day=row['day']
        entry=flow[day,issued]
        entry['selected_cells']+=1
        reason=None
        if unresolved(audit,row):
            entry['unresolved_clock']+=1
            if route!=row['route_id']:
                route=row['route_id']
                audit.load_route(route)
            reason,hint=audit.evaluate(row)
            if hint:
                entry['hints']+=1
                replayed[compact(hint)]+=1
        move_counts(staged,day,issued,reason)
    if expected!=replayed:
        raise ValueError('A date-crossing observation differs from its independent receipt-time replay')
    daily={}
    for day,values in staged.items():
        counts,rows=values['coverage'],values['rows']
        if (min(counts.values(),default=0)<0 or counts['movement_hint']!=len(rows) or
                any(counts[key]!=observed[day][key] for key in ('route_hour_candidates','unresolved_clock')) or
                sum(n for k,n in counts.items() if k not in ('route_hour_candidates','unresolved_clock'))!=counts['unresolved_clock']):
            raise ValueError('Issue-date denominators failed to reconcile at '+day)
        daily[day]={'coverage':dict(counts),'verified':sum('upper_s' in r for r in rows),'hints':len(rows),
                    'route_ids_with_selected_cell':len(routes[day]),'route_ids_with_unresolved_clock':len(unresolved_routes[day])}
        if day in ranges:
            daily[day]['first_last_receipt_ist']=[datetime.fromtimestamp(t,IST).isoformat() for t in ranges[day]]
    source_edges={'first_available_readiness_date':MONTH[0],'last_available_readiness_date':MONTH[-1],
                  'september_30_receipts_in_selected_cells':observed['2026-09-30']['route_hour_candidates'],
                  'november_1_readiness_cells_available':False,
                  'outside_month_receipts':{d:daily[d] for d in sorted(daily) if d not in MONTH}}
    flows=[{'readiness_date':r,'receipt_date':i,**v} for (r,i),v in sorted(flow.items())]
    for name,days in (('development',DEV),('confirmation',CONFIRM),('report',MONTH)):
        result=aggregate(staged,days,evaluator,policy)
        result.update(protocol='unassigned-clock-observation-receipt-confirmation-v1',partitioner_sha256=partitioner,
                      partition='forecast_receipt_ist',source_readiness_edges=source_edges,
                      per_receipt_date={d:daily[d] for d in days},
                      date_crossings=[v for v in flows if v['readiness_date'] in days or v['receipt_date'] in days],
                      route_ids_with_selected_cell=len(set().union(*(routes[d] for d in days))),
                      route_ids_with_unresolved_clock=len(set().union(*(unresolved_routes[d] for d in days))))
        save(output/('receipt-'+name+'.json'),result)
        print(name,len(days),'receipt dates',result['coverage']['unresolved_clock'],'unresolved',result['total']['hints'],'observations')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,default=ROOT/'data/fleet-cases')
    parser.add_argument('--output',type=Path,default=ROOT/'data/fleet-anonymous-arrivals')
    args=parser.parse_args()
    publish(args.input,args.output)
