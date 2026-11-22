"""Independently reconcile published case counts, temporal cutoffs and receipt-tie rates."""
import argparse
from collections import Counter
import gzip
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def check(path, prior, fields, signature):
    with gzip.open(path,'rt',encoding='utf-8') as f:
        report = json.load(f)
    day = report['day']
    with gzip.open(prior/f'{day}.json.gz','rt',encoding='utf-8') as f:
        census = json.load(f)['census']
    if not report['complete'] or report['signature']!=signature or len(report['hours'])!=24:
        raise ValueError('Incomplete or mismatched fleet report for '+day)
    if report['cell_fields'] != fields:
        raise ValueError('Unexpected cell column order')
    by_route,expected,matched = Counter(),set(),set()
    for hour,cells in enumerate(report['hours']):
        raw = 0
        for route,(broadcasts,usable,unique,forward,near) in cells.items():
            if not 0<=unique<=usable<=broadcasts or not 0<=near<=forward:
                raise ValueError('Invalid funnel counts')
            raw += broadcasts
            by_route[route]+=broadcasts
            if near:
                expected.add((hour,route))
        if raw!=census['by_hour'].get(f'{hour:02d}',0):
            raise ValueError('Raw hour disagrees with independent census')
    if +by_route!=Counter(census['by_route']):
        raise ValueError('Raw route IDs disagree with independent census')
    counts = Counter()
    for row in report['cases']:
        h,r = key = (row['hour'],row['route_id'])
        if key in matched or key not in expected:
            raise ValueError('Duplicate or orphan selected route-hour')
        matched.add(key)
        if row['ready']-row['forecast']!=row['gap'] or not 1<=row['gap']<=30 or row['forecast_receipt']>=row['ready']:
            raise ValueError('Inconsistent forecast chronology')
        bracket,received = row['gps_entry_exit'],row['gps_entry_exit_receipts']
        label = 'unverified'
        if bracket is not None:
            before,entered,left = bracket
            if before>=entered or (left is not None and left<=entered) or not all(
                    o is None and t is None or o is not None and t is not None and 0<=t-o<=90
                    for o,t in zip(bracket,received)):
                raise ValueError('Impossible GPS entry/exit bracket')
            label = ('approaching' if row['ready']<=before else
                     'gone' if left is not None and row['ready']>=left else 'uncertain')
        if label != row['outcome']:
            raise ValueError('Outcome differs from independent GPS bracket')
        receipts = Counter()
        fixes = set()
        for t,obs,y,x in row['observations']:
            if not obs<=t<=row['ready'] or t-obs>90:
                raise ValueError('Observation unavailable at issue')
            receipts[t]+=1
            fixes.add((t,obs,y,x))
        eligible = bool(row['eligible_named_case'])
        pool = 'eligible' if eligible else 'unmatched'
        counts[pool]+=1
        if max(receipts.values(),default=0)>1:
            counts[pool+'_cases_with_tied_receipts']+=1
        if len({t for t,_,_,_ in fixes})<len(fixes):
            counts[pool+'_cases_with_distinct_fixes_in_one_receipt']+=1
        if not eligible and row['candidates_checked']!=report['hours'][h][r][-1]:
            raise ValueError('An unmatched route-hour did not exhaust every candidate')
    if matched!=expected:
        raise ValueError('An eligible route-hour lacks a case')
    return dict(counts)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases',type=Path,default=ROOT/'data/fleet-cases')
    parser.add_argument('--census',type=Path,default=ROOT/'data/month-boarding')
    parser.add_argument('--days',nargs='+')
    parser.add_argument('--json',type=Path,help='Write the auditor counts and signature for all 31 dates.')
    args=parser.parse_args()
    manifest=json.loads((args.cases/'manifest.json').read_text())
    paths=([args.cases/f'{day}.json.gz' for day in args.days] if args.days else sorted(args.cases.glob('*.json.gz')))
    total,days=Counter(),{}
    for path in paths:
        result=check(path,args.census,manifest['cell_fields'],manifest['signature'])
        print(path.name,json.dumps(result,sort_keys=True),flush=True)
        total.update(result)
        days[path.name.removesuffix('.json.gz')]=result
    print('All checked dates',len(paths),json.dumps(total,sort_keys=True))
    if args.json:
        if set(days)!={f'2026-10-{d:02d}' for d in range(1,32)}:
            raise ValueError('An October audit artifact requires every date.')
        report={'protocol':manifest['protocol'],'signature':manifest['signature'],
                'days':dict(sorted(days.items())),'counts':dict(total)}
        tmp=args.json.with_suffix('.pending')
        tmp.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
        tmp.replace(args.json)
