"""Replay held-out October GPS forecasts as single named stop updates in the journey router."""

import argparse
from collections import Counter, defaultdict
from datetime import date, datetime
from functools import lru_cache
import gzip
import hashlib
import json
from pathlib import Path
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.near_miss_peak import LABELS, nominal_start_minute, token
from tools.train_eta import boundary, clock, format_clock, open_db
from transit.eta import EtaModel, IST, read_data
from transit.live import TTL, protobuf
from transit.replay import CapturedOverlay
from transit.router import MINIMUM_ALERT_GAIN_SECONDS, Router
from transit.walking import Walking


def prepared_rows(db, rows, static_gate=None):
    at_row = lru_cache(maxsize=None)(lambda s: boundary(db,format_clock(s)))
    prepared = []
    for row in rows:
        issue = row['ready']
        midnight = int(datetime.fromtimestamp(issue,IST).replace(hour=0,minute=0,second=0,microsecond=0).timestamp())
        receipt = row['forecast_receipt'] - midnight
        # Start with the original polling second and physical/run identity, independent of the eventual label.
        hits = db.execute('SELECT vehicle_id,trip_id FROM vehicle_feed WHERE rowid>=? AND rowid<? '
                          'AND route_id=? AND CAST(timestamp AS INT)=? AND time=?',
                          (at_row(receipt),at_row(receipt+1),int(row['route_id']), row['origin_gps'],format_clock(receipt)))
        ids = {(str(bus),str(trip)) for bus,trip in hits if token(bus,'bus')==row['bus'] and
               token(row['day'],bus,trip)==row['run']}
        if len(ids) != 1:
            prepared.append((row, 'raw_anchor_not_unique', None))
            continue
        bus, trip = ids.pop()
        start_minute = nominal_start_minute(row['route_id'],trip)
        if start_minute is None:
            prepared.append((row, 'unrecognized_trip_clock', None))
            continue
        if static_gate:
            static_reason = static_gate(row, start_minute)
            if static_reason:
                prepared.append((row,static_reason,None))
                continue
        # Keep only reports received before readiness. Other recent claimants make an inferred vehicle assignment unsafe.
        since = max(0, issue - midnight - TTL)
        fixes = db.execute('SELECT vehicle_id,trip_id,CAST(timestamp AS INT),time,CAST(lat AS REAL),CAST(lng AS REAL),route_id '
                           'FROM vehicle_feed WHERE rowid>=? AND rowid<? AND (route_id=? OR vehicle_id=?) '
                           'AND CAST(timestamp AS INT) BETWEEN ? AND ?',
                           (at_row(since),at_row(issue-midnight+1),int(row['route_id']),bus,issue-TTL,issue))
        active_vehicles = set()
        observations = {}
        reassigned = False
        for v,t,obs,ts,lat,lon,rt in fixes:
            received = midnight+clock(ts)
            if (obs is None or lat is None or lon is None or not 0 <= received-obs <= 90
                or not (28<=lat<=30 and 76<=lon<=78) or received>issue):
                continue
            rt = str(int(rt)) if rt is not None else ''
            if rt==row['route_id'] and nominal_start_minute(rt,str(t))==start_minute:
                active_vehicles.add(str(v))
            if str(v)==bus and obs>=row['origin_gps']:
                if rt!=row['route_id'] or str(t)!=trip:
                    reassigned = True
                else:
                    observations.setdefault((received,obs),(lat,lon))
        if reassigned or len(active_vehicles)>1:
            reason = 'vehicle_reassigned_before_issue' if reassigned else 'multiple_recent_vehicles_claim_clock'
            prepared.append((row,reason,None))
        else:
            prepared.append((row,None,(start_minute,observations)))
    return prepared


def matched_trip(router, registry, row, start_minute):
    day = date.fromisoformat(row['day'])
    variants = [(pid,k) for pid,k in registry.by_clock.get((row['route_id'],start_minute),())
                if router.patterns[pid].service in router._active(day)]
    if not variants:
        return 'no_active_trip_at_start_minute',None
    calls = [row['upstream'],row['stop'],row['next_stop']]
    options = [(p,k,i) for pid,k in variants for p in [router.patterns[pid]] for i,s in enumerate(p.stops)
               if s==calls[0] and p.stops[i:i+3]==calls]
    if len(options)!=1:
        return ('ambiguous_static_pattern' if options else 'ordered_stop_pattern_mismatch'),None
    p,k,i = options[0]
    if p.pickups[i+1] or p.drops[i+2]:
        return 'boarding_or_alighting_restricted',None
    return None,p.trips[k]


class BeforeExitVeto(CapturedOverlay):
    """Offline comparison only. All other routing behavior uses the production code."""
    def passed_platform(self, day, pid, index, identity, asof):
        return False


class CountedExitVeto(CapturedOverlay):
    """Observe the unchanged production guard, to identify precisely when a second run is needed."""
    veto_reached = False

    def passed_platform(self, day, pid, index, identity, asof):
        decision = super().passed_platform(day,pid,index,identity,asof)
        if decision:
            self.veto_reached = True
        return decision


def simulated_overlay(model, data, registry, row, trip, observations, overlay_type):
    issue = row['ready']
    overlay = overlay_type(model,data,issue)
    # Only static lookup tables are shared. Every case starts with empty live state.
    overlay.router,overlay.ids,overlay.by_clock,overlay.edges = registry.router,registry.ids,registry.by_clock,registry.edges
    def wire(stamp):
        f = protobuf().FeedMessage()
        f.header.gtfs_realtime_version = '2.0'
        f.header.timestamp = stamp
        return f
    def identity(item):
        item.vehicle.id = row['bus']
        item.trip.route_id = row['route_id']
        item.trip.trip_id = trip
        item.trip.start_date = row['day'].replace('-','')
    forecast = wire(row['forecast_receipt'])
    update = forecast.entity.add(id='simulated').trip_update
    identity(update)
    update.timestamp = row['forecast_receipt']
    update.stop_time_update.add(stop_id=row['stop']).departure.time = row['forecast']
    overlay.apply_trip_updates(forecast,row['forecast_receipt'])
    for (receipt,obs),(lat,lon) in sorted(observations.items()):
        report=wire(receipt)
        v=report.entity.add(id='observed').vehicle
        identity(v)
        v.timestamp=obs
        v.position.latitude,v.position.longitude=lat,lon
        overlay.apply_positions(report,receipt,provider='community_positions')
    return overlay


def replay_one(router,registry,model,data,row,trip,observations,overlay_type):
    issue,stop = row['ready'],row['stop']
    overlay = simulated_overlay(model,data,registry,row,trip,observations,overlay_type)
    accepted = overlay.departure(trip,stop,date.fromisoformat(row['day']),row['forecast'],issue)==row['forecast']
    dt = datetime.fromtimestamp(issue,IST)
    answer = router.journeys(stop,row['next_stop'],dt,asof=dt,eta=overlay)
    selected = [a for a in answer['boarding_alerts'] if a['trip_id']==trip and a['platform']['id']==stop and
                a['alighting_stop']['id']==row['next_stop'] and a.get('conditional_itinerary')]
    result = {'static_trip':trip,'forecast_accepted_at_issue':accepted,
              'strict_seconds':answer['journeys'][0]['duration_seconds'] if answer['journeys'] else None,
              'conditional_emitted':bool(selected)}
    if overlay_type in (CountedExitVeto,BeforeExitVeto):
        result['exit_veto_reached'] = getattr(overlay,'veto_reached',False)
        if not selected:
            alerts = [a for a in answer['boarding_alerts'] if a['trip_id']==trip and a['platform']['id']==stop]
            if not accepted:
                reason = 'named_forecast_unavailable_at_issue'
            elif result['strict_seconds'] is None:
                reason = 'no_strict_transit_fallback'
            elif any(a['alighting_stop']['id']==row['next_stop'] for a in alerts):
                reason = 'no_qualifying_same_platform_fallback'
            elif alerts:
                reason = 'other_alighting_stop_selected'
            elif result['strict_seconds'] <= MINIMUM_ALERT_GAIN_SECONDS:
                reason = 'ordinary_transit_already_within_gain_floor'
            else:
                reason = 'no_qualifying_direct_alert'
            result['offer_reason'] = reason
    if selected:
        a=selected[0]
        assert a['catchable'] is False and a['seconds_before_ready']==row['gap']
        assert a['predicted_departure']==datetime.fromtimestamp(row['forecast'],IST).isoformat()
        result.update(conditional_seconds=a['conditional_itinerary']['duration_seconds'],
                      same_platform_fallback_seconds=a['fallback_itinerary']['duration_seconds'])
    return result


def counts(rows):
    emitted=[r for r in rows if r.get('conditional_emitted')]
    breakdown=lambda rr:{k:sum(r['outcome']==k for r in rr) for k in LABELS}
    return {'attempted':len(rows),'attempted_routes':len({r['route_id'] for r in rows}),
            'statically_matched_and_replayed':sum('static_trip' in r for r in rows),
            'replayed_routes':len({r['route_id'] for r in rows if 'static_trip' in r}),
            'accepted_live_call_at_issue':sum(r.get('forecast_accepted_at_issue',False) for r in rows),
            'conditional_emitted':len(emitted),'emitted_routes':len({r['route_id'] for r in emitted}),
            'definitive_emitted':sum(r['outcome'] in ('approaching','gone') for r in emitted),
            'attempted_outcomes':breakdown(rows),'emitted_outcomes':breakdown(emitted),
            'exclusions':dict(Counter(r['excluded'] for r in rows if 'excluded' in r))}


def dated_exit_receipts(db, output, source):
    """After both policies run, look up receipt clocks independently of their decisions."""
    checked = []
    at_row = lru_cache(maxsize=None)(lambda s: boundary(db,format_clock(s)))
    for case in output:
        if not case.get('baseline_conditional_emitted') or case['outcome'] != 'gone':
            continue
        raw = source[case['day'],case['run']]
        _,entry,exit_time = raw['gps_entry_exit']
        ready = case['ready']
        midnight = int(datetime.fromtimestamp(ready,IST).replace(hour=0,minute=0,second=0,microsecond=0).timestamp())
        receipts = []
        for obs in (entry,exit_time):
            lo,hi = at_row(obs-midnight),at_row(obs-midnight+91)
            matches = db.execute('SELECT vehicle_id,trip_id,time FROM vehicle_feed WHERE rowid>=? AND rowid<? '
                                 'AND route_id=? AND CAST(timestamp AS INT)=?',(lo,hi,int(case['route_id']),obs))
            receipts.append(min((midnight+clock(t) for bus,trip,t in matches if token(bus,'bus')==case['bus']
                                 and token(case['day'],bus,trip)==case['run']),default=None))
        checked.append({'day':case['day'],'route_id':case['route_id'],'run':case['run'],'ready':ready,
                        'entry_observed':entry,'entry_received':receipts[0],
                        'exit_observed':exit_time,'exit_received':receipts[1],
                        'both_received_by_ready':max(receipts)<=ready if None not in receipts else None,
                        'suppressed':not case['conditional_emitted']})
    return checked


def exit_receipts(directory, output, source):
    checked = []
    for day in sorted({r['day'] for r in output}):
        with open_db(directory / f"bus_movements_{day.replace('-','_')}.db") as db:
            checked.extend(dated_exit_receipts(db,[r for r in output if r['day']==day],source))
    return checked


def load_prospective_profiles(data_dir):
    """Load both frozen production priors without historical evaluation receipts."""
    directory = Path(data_dir)
    model = EtaModel(read_data(directory/'eta_model.json.gz'))
    model.weekend = EtaModel(read_data(directory/'weekend'/'eta_model.json.gz'))
    return model


MONTH_PARTS = ('AM','noon','PM','night')


def month_counts(rows):
    """GPS evidence and both offers and misses retain all four outcome classes."""
    result = counts(rows)
    stages = {}
    for key,group in (
        ('accepted', [r for r in rows if r.get('forecast_accepted_at_issue')]),
        ('excluded', [r for r in rows if r.get('excluded')]),
        ('not_offered', [r for r in rows if not r.get('conditional_emitted')]),
        ('suppressed_by_exit', [r for r in rows if r.get('baseline_conditional_emitted') and not r.get('conditional_emitted')]),
    ):
        stages[key] = {label:sum(r['outcome']==label for r in group) for label in LABELS}
    result['outcomes_by_stage'] = stages
    result['decision_reasons'] = dict(Counter(r.get('excluded') or r.get('offer_reason','offered') for r in rows))
    return result


class MonthRouter:
    """A warm production network and one frozen as-of model for a paired month study."""
    def __init__(self, root=ROOT, model=None):
        self.data = Path(root)/'data'
        self.model = model if model is not None else load_prospective_profiles(self.data)
        if self.model.weekend is None or self.model.last_train_day is None or self.model.weekend.last_train_day is None:
            raise ValueError("Both dated weekday and weekend profiles are required")
        if any(p.passages or p.recent for p in (self.model,self.model.weekend)):
            raise ValueError("The month study requires fixed profiles without validation replay receipts")
        self.router = Router(Path(root)/'delhi_buses_static_gtfs_v1.zip',self.data,Walking(self.data),self.model)
        self.registry = CapturedOverlay(self.model,self.data,0).bind(self.router)

    def evaluate_day(self, day, opportunities, db_path):
        day = date.fromisoformat(day) if isinstance(day,str) else day
        start = perf_counter()
        rows = []
        seen = set()
        gaps = Counter()
        for row in opportunities:
            if row['day']!=day.isoformat() or row['peak'] not in MONTH_PARTS:
                raise ValueError("Mismatched service date or daypart in month opportunities")
            if row['forecast']+row['gap']!=row['ready'] or row['outcome'] not in LABELS:
                raise ValueError("Inconsistent readiness gap or GPS outcome")
            gaps[row['peak']] += 1
            if 1<=row['gap']<=30:
                key = row['peak'],row['bus'],row['run']
                if key in seen:
                    raise ValueError("Duplicate dated vehicle/run in the same daypart")
                seen.add(key)
                rows.append(row)
        output,nominal = [],{}
        def gate(row,minute):
            key = row['route_id'],minute,row['upstream'],row['stop'],row['next_stop']
            if key not in nominal:
                nominal[key] = matched_trip(self.router,self.registry,row,minute)
            return nominal[key][0]
        candidates, prepared = [],[]
        for row in rows:
            if (row['origin_gps']<=row['forecast_receipt']<=row['ready'] and
                datetime.fromtimestamp(row['ready'],IST).date()==day):
                candidates.append(row)
            else:
                prepared.append((row,'invalid_issue_or_receipt_clock',None))
        with open_db(Path(db_path)) as db:
            prepared.extend(prepared_rows(db,candidates,gate))
            scan_seconds = perf_counter()-start
            second_runs = 0
            for row,reason,case in prepared:
                result = {k:row[k] for k in ('day','peak','bus','run','route_id','upstream','stop','next_stop',
                                              'ready','gap','forecast_receipt','forecast','outcome')}
                if 'forecast_source' in row:
                    result['forecast_source'] = row['forecast_source']
                if reason:
                    result['excluded'] = reason
                else:
                    minute,observations = case
                    trip = nominal[row['route_id'],minute,row['upstream'],row['stop'],row['next_stop']][1]
                    args = self.router,self.registry,self.model,self.data,row,trip,observations
                    revised = replay_one(*args,CountedExitVeto)
                    # An identical branch outcome in every production call makes the old search identical.
                    # When the guard ever fires, rerun the complete production query with only that guard disabled.
                    if revised['exit_veto_reached']:
                        second_runs += 1
                        baseline = replay_one(*args,BeforeExitVeto)
                    else:
                        baseline = revised
                    if (baseline['strict_seconds']!=revised['strict_seconds'] or
                        baseline['forecast_accepted_at_issue']!=revised['forecast_accepted_at_issue'] or
                        revised['conditional_emitted'] and not baseline['conditional_emitted']):
                        raise AssertionError("Exit guard changed strict routing or introduced a conditional offer")
                    result.update(revised,baseline_conditional_emitted=baseline['conditional_emitted'],
                                  baseline_offer_reason=baseline.get('offer_reason','offered'),
                                  position_reports_received=len(observations))
                    if baseline['conditional_emitted']:
                        result.update(baseline_conditional_seconds=baseline['conditional_seconds'],
                                      baseline_same_platform_fallback_seconds=baseline['same_platform_fallback_seconds'])
                        if not revised['conditional_emitted']:
                            result['offer_reason'] = 'confirmed_platform_exit'
                output.append(result)
            source = {(r['day'],r['run'],r['peak']):r for r in rows}
            audit = []
            for part in MONTH_PARTS:
                section = [r for r in output if r['peak']==part]
                audit.extend(dated_exit_receipts(db,section,{(r['day'],r['run']):source[r['day'],r['run'],part] for r in section}))
        baseline = [dict(r,conditional_emitted=r.get('baseline_conditional_emitted',False),
                         offer_reason=r.get('baseline_offer_reason','offered')) for r in output]
        def summarize(rr):
            return {'overall':month_counts(rr),'by_peak':{p:month_counts([r for r in rr if r['peak']==p]) for p in MONTH_PARTS}}
        profile = self.model.weekend if day.weekday()>=5 else self.model
        return {'day':day.isoformat(), **summarize(output),'baseline':summarize(baseline),'rows':output,
                'gone_offer_receipt_audit':audit,'elapsed_seconds':round(perf_counter()-start,3),
                'diagnostics':{'all_signed_gap_opportunities':sum(gaps.values()),'all_opportunities_by_peak':dict(gaps),
                               'profile':'weekday' if day.weekday()<5 else 'weekend',
                               'profile_last_train_date':profile.last_train_day.isoformat(),
                               'historical_prior_available':day>profile.last_train_day,
                               'raw_matching_seconds':round(scan_seconds,3),'extra_baseline_queries':second_runs}}


def main(args):
    data = ROOT / 'data'
    input_rows = args.opportunities
    with gzip.open(input_rows,'rt',encoding='utf-8') as src:
        subset=[r for r in json.load(src) if r['day'] in args.days and 1<=r['gap']<=30]
    grouped=defaultdict(list)
    for row in subset:
        grouped[row['day']].append(row)
    prepared=[]
    for day,rows in grouped.items():
        with open_db(args.directory/f"bus_movements_{day.replace('-','_')}.db") as db:
            prepared.extend(prepared_rows(db,rows))
        print(f'{day}: read {len(rows)} opportunities', flush=True)
    print('Eligibility scans complete.',flush=True)
    model=EtaModel(read_data(data/'eta_model.json.gz'))
    router=Router(ROOT/'delhi_buses_static_gtfs_v1.zip',data,Walking(data),model)
    registry=CapturedOverlay(model,data,0).bind(router)
    output=[]
    for n,(row,reason,case) in enumerate(prepared,1):
        result={k:row[k] for k in ('day','peak','bus','run','route_id','upstream','stop','next_stop','ready','gap','outcome')}
        if reason is None:
            start,observations=case
            reason,trip=matched_trip(router,registry,row,start)
            if trip:
                before = replay_one(router,registry,model,data,row,trip,observations,BeforeExitVeto)
                after = replay_one(router,registry,model,data,row,trip,observations,CapturedOverlay)
                assert before['strict_seconds']==after['strict_seconds'] and before['forecast_accepted_at_issue']==after['forecast_accepted_at_issue']
                assert not after['conditional_emitted'] or before['conditional_emitted']
                result.update(after,baseline_conditional_emitted=before['conditional_emitted'])
                if before['conditional_emitted']:
                    result['baseline_conditional_seconds'] = before['conditional_seconds']
                    result['baseline_same_platform_fallback_seconds'] = before['same_platform_fallback_seconds']
        if reason:
            result['excluded']=reason
        output.append(result)
        if n%50==0:
            print(f'Replayed or classified {n}/{len(prepared)} cases',flush=True)
    diagnosis=exit_receipts(args.directory,output,{(r['day'],r['run']):r for r in subset})
    print('All scratch scans complete.',flush=True)
    def summary(rows):
        return {'overall':counts(rows),'by_peak':{p:counts([r for r in rows if r['peak']==p]) for p in ('AM','PM')},
                'by_day':{d:counts([r for r in rows if r['day']==d]) for d in grouped}}
    baseline=[dict(r, conditional_emitted=r.get('baseline_conditional_emitted',False)) for r in output]
    report={'opportunities_sha256':hashlib.sha256(input_rows.read_bytes()).hexdigest(),
            'days':args.days, **summary(output),'baseline':summary(baseline),'gone_offer_receipt_audit':diagnosis,'rows':output}
    args.output.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report['overall']),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory',type=Path,help='Directory containing the previously extracted held-out SQLite archives.')
    parser.add_argument('--opportunities',type=Path,default=ROOT/'data'/'near-miss-opportunities.json.gz')
    parser.add_argument('--days',nargs='+',default=['2026-10-23','2026-10-26'])
    parser.add_argument('--output',type=Path,default=ROOT/'data'/'near-miss-router.json')
    main(parser.parse_args())
