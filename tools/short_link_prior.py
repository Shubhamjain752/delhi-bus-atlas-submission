"""Fit a prospective route-link travel-time prior before 24 October, then score the last eight days."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime
import gzip
import hashlib
import json
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from transit.eta import IST

CUTOFF = int(datetime(2026,10,24,tzinfo=IST).timestamp())
END = int(datetime(2026,11,1,tzinfo=IST).timestamp())
MINIMUM_DATES = 3
TIMETABLE_WEIGHT = 3


def link(e):
    return str(e['route']),e['from_stop'],e['to_stop']


def departure(e):
    return (e['departure_lower']+e['departure_upper'])//2


def fit(events):
    samples = defaultdict(lambda:defaultdict(list))
    for e in events:
        if e['issue']>=CUTOFF or e.get('entry_received',CUTOFF)>=CUTOFF:
            continue
        seconds = (e['entry_lower']+e['entry_upper'])/2-departure(e)
        if seconds<=0:
            continue
        day = datetime.fromtimestamp(e['issue'],IST).date().isoformat()
        samples[link(e)][day].append(seconds)
    profiles = {}
    for route_link,by_day in samples.items():
        if len(by_day)>=MINIMUM_DATES:
            profiles[route_link] = {'dates':sorted(by_day),'entries':sum(map(len,by_day.values())),
                                    'median_seconds':statistics.median(statistics.median(v) for v in by_day.values())}
    return profiles


def score_event(e,profiles):
    profile = profiles.get(link(e))
    prediction = e['schedule_forecast']
    if profile:
        n = len(profile['dates'])
        origin = departure(e)
        scheduled = prediction-origin
        estimate = (n*profile['median_seconds']+TIMETABLE_WEIGHT*scheduled)/(n+TIMETABLE_WEIGHT)
        prediction = origin+max(1,round(estimate))
    return {**e,'experimental_forecast':prediction,'profile':bool(profile)}


def accuracy(values,field):
    verified = [r for r in values if 'entry_lower' in r]
    if not verified:
        return {'verified':0}
    signed = [r[field]-(r['entry_upper']+r['entry_lower'])/2 for r in verified]
    absolute = list(map(abs,signed))
    return {'verified':len(verified),'median_absolute_s':statistics.median(absolute),
            'mean_absolute_s':round(statistics.mean(absolute),2),'median_bias_s':statistics.median(signed),
            'within_60s_midpoint':sum(err<=60 for err in absolute),
            'over_120s_early':sum(x< -120 for x in signed),'over_120s_late':sum(x>120 for x in signed),
            'within_60s_bracket':sum(r['entry_lower']-60<=r[field]<=r['entry_upper']+60 for r in verified)}


def summary(rows):
    counts = Counter()
    for row in rows:
        if 'entry_lower' not in row:
            continue
        arrival = (row['entry_upper']+row['entry_lower'])/2
        difference = abs(row['experimental_forecast']-arrival)-abs(row['schedule_forecast']-arrival)
        counts['improved' if difference<0 else 'worsened' if difference>0 else 'equal']+=1
    return {'issued':len(rows),'covered_issued':sum(r['profile'] for r in rows),
            'covered_verified':sum(r['profile'] and 'entry_lower' in r for r in rows),
            'timetable_fallback':sum(not r['profile'] for r in rows),'unknown_outcome':sum('entry_lower' not in r for r in rows),
            'experimental':accuracy(rows,'experimental_forecast'),'timetable':accuracy(rows,'schedule_forecast'),
            'current_eta':accuracy(rows,'forecast'),'paired_vs_timetable':dict(counts),
            'experimental_eta_already_past_at_issue':sum(r['experimental_forecast']<=r['issue'] for r in rows)}


def run(directory):
    report = json.loads((directory/'fleet-short-links.json').read_text())
    if not report['complete']:
        raise ValueError('Full 31-date short-route replay required')
    path = directory/'fleet-short-links-events.json.gz'
    with gzip.open(path,'rt',encoding='utf-8') as stream:
        events = json.load(stream)
    if len(events)!=report['total']['issued']:
        raise ValueError('Aggregate and event denominators differ')
    profiles = fit(events)
    rows = [score_event(e,profiles) for e in events if CUTOFF<=e['issue']<END]
    if len(rows)!=report['holdout_24_31']['issued']:
        raise ValueError('Date partition differs from published aggregate')
    route_rows = defaultdict(list)
    for row in rows:
        route_rows[str(row['route'])].append(row)
    payload = {'protocol':'short-link-operating-prior-v1','training_ends_before':'2026-10-24T00:00:00+05:30',
               'confirmation_days':report['days'][23:],'minimum_dates':MINIMUM_DATES,'timetable_weight':TIMETABLE_WEIGHT,
               'complete':True,'route_links_with_prior':len(profiles),'route_ids_with_prior':len({k[0] for k in profiles}),
               'short_study_sha256':report['script_sha256'],'event_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
               'evaluator_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
               'holdout':summary(rows),'covered_holdout':summary([r for r in rows if r['profile']]),
               'two_stop':summary([r for r in rows if r['stops']==2]),'three_stop':summary([r for r in rows if r['stops']==3]),
               'after_midnight_00_06':summary([r for r in rows if datetime.fromtimestamp(r['issue'],IST).hour<6]),
               'by_route':{rt:summary(v) for rt,v in sorted(route_rows.items())},
               'profiles':[{'route_id':r,'from_stop':a,'to_stop':b,**v} for (r,a,b),v in sorted(profiles.items())]}
    target = directory/'fleet-short-link-prior.json'
    part = target.with_suffix('.pending')
    part.write_text(json.dumps(payload,allow_nan=False,separators=(',',':')),encoding='utf-8')
    part.replace(target)
    print(json.dumps(payload['holdout']),flush=True)


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data',type=Path,default=ROOT/'data')
    run(parser.parse_args().data)
