"""Cross-check October platform offers against ticket creation, using only exact match fields."""

import argparse
from collections import Counter, defaultdict
import csv
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from functools import lru_cache
import gzip
import hashlib
import io
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tools.near_miss_peak import token
from transit.eta import IST
from transit.router import Router

MONTH = tuple(f'2026-10-{d:02}' for d in range(1,32))
WINDOWS = (60,300)
POLICIES = ('baseline','revised')
FIELDS = ('passenger_count','amount','vehicle_number','ticket_type_id','end_stop_name',
          'route','start_stop_name','created_at','is_ac')


def name(value):
    return ' '.join(value.casefold().split())


def missing(value):
    return not value.strip() or value.strip().upper()=='NULL'


@lru_cache(maxsize=20000)
def bus_token(raw):
    return token(raw,'bus')


@lru_cache(maxsize=8192)
def fare_kind(raw):
    try:
        value=Decimal(raw)
    except InvalidOperation:
        return 'invalid'
    if not value.is_finite():
        return 'invalid'
    return 'negative' if value<0 else 'zero' if value==0 else 'positive'


@dataclass
class Offer:
    day: str
    bus: str
    route: str
    stop: str
    ready: int
    policies: tuple
    outcome: str
    reference_error: str | None = None
    bus_seen: bool = False
    route_seen: bool = False
    stop_seen: bool = False
    near: dict = field(default_factory=lambda:{w:set() for w in WINDOWS})
    unlinked: dict = field(default_factory=lambda:{w:set() for w in WINDOWS})


def load_offers(root, partial=False):
    dates_dir=root/'data'/'month-boarding'
    router=Router(root/'delhi_buses_static_gtfs_v1.zip',root/'data',None,None)
    names=defaultdict(set)
    for pattern in router.patterns:
        label=name(router.routes[pattern.route][0])
        for sid in set(pattern.stops):
            names[label,name(router.stops[sid]['name'])].add(sid)
    manifest=json.loads((dates_dir/'manifest.json').read_text(encoding='utf-8'))
    dates,offers,digests=[],[],{}
    for day in MONTH:
        path=dates_dir/(day+'.json.gz')
        if not path.is_file() and partial:
            continue
        with gzip.open(path,'rt',encoding='utf-8') as stream:
            item=json.load(stream)
        if not item['complete'] or item['day']!=day or item['signature']!=manifest['signature']:
            raise ValueError('Invalid month study shard '+day)
        dates.append(day)
        digests[day]=hashlib.sha256(path.read_bytes()).hexdigest()
        seen=set()
        for r in item['router']['rows']:
            baseline,revised=bool(r.get('baseline_conditional_emitted')),bool(r.get('conditional_emitted'))
            if revised and not baseline:
                raise ValueError('Revised offer missing from baseline')
            if not baseline:
                continue
            key=(day,r['bus'],r['ready'],r['stop'])
            if key in seen:
                raise ValueError('Duplicate physical offer in a date shard')
            seen.add(key)
            route=name(router.routes.get(r['route_id'],('',''))[0])
            stop=name(router.stops.get(r['stop'],{}).get('name',''))
            plausible=names.get((route,stop),set())
            error=None
            if not route or not stop or r['stop'] not in plausible:
                error='missing_official_route_or_platform'
            elif len(plausible)!=1:
                error='ambiguous_boarding_name_on_route'
            offers.append(Offer(day,r['bus'],route,stop,r['ready'],POLICIES if revised else ('baseline',),
                                r['outcome'],error))
    return dates,offers,manifest['signature'],digests


def inspect_stream(stream,offers):
    rows=csv.reader(stream)
    if tuple(next(rows))!=FIELDS:
        raise ValueError('Unexpected ticket archive header')
    columns={f:Counter() for f in FIELDS}
    dates=defaultdict(Counter)
    tz=Counter()
    count=Counter()
    values={k:Counter() for k in ('passenger_count','amount','ticket_type_id','is_ac')}
    registrations,routes,stops=set(),set(),set()
    by_bus_day=defaultdict(list)
    by_place_day=defaultdict(list)
    for offer in offers:
        if not offer.reference_error:
            by_bus_day[(offer.day,offer.bus)].append(offer)
            by_place_day[(offer.day,offer.route,offer.stop)].append(offer)
    results={(p,w):Counter() for p in POLICIES for w in WINDOWS}
    contents={(p,w):set() for p in POLICIES for w in WINDOWS}
    for cells in rows:
        count['csv_records']+=1
        if len(cells)!=len(FIELDS):
            count['incorrect_field_count']+=1
            continue
        passenger,amount,vehicle,kind,_,route,board,created,ac=cells
        for key,value in zip(FIELDS,cells):
            columns[key]['blank' if not value.strip() else 'null_literal' if value.strip().upper()=='NULL' else 'present']+=1
        has_vehicle,has_route,has_stop=not missing(vehicle),not missing(route),not missing(board)
        values['passenger_count']['positive_integer' if passenger.isascii() and passenger.isdecimal() and int(passenger)>0
                                  else 'other']+=1
        values['amount'][fare_kind(amount)]+=1
        values['ticket_type_id']['integer' if kind.isascii() and kind.isdecimal() else 'other']+=1
        ac_value=ac.strip().casefold()
        values['is_ac'][ac_value if ac_value in ('true','false') else 'other']+=1
        count['vehicle_rows_with_edge_whitespace']+=has_vehicle and vehicle!=vehicle.strip()
        route_name=name(route) if has_route else ''
        stop_name=name(board) if has_stop else ''
        if has_route:
            routes.add(route_name)
        if has_stop:
            stops.add(stop_name)
        if has_vehicle:
            registrations.add(bus_token(vehicle))
        try:
            stamp=datetime.fromisoformat(created)
        except ValueError:
            count['invalid_or_missing_created_at']+=1
            continue
        if stamp.utcoffset() is None:
            count['created_at_without_offset']+=1
            continue
        tz[str(stamp.utcoffset())]+=1
        local=stamp.astimezone(IST)
        day=local.date().isoformat()
        focus=dates[day]
        focus['dated_ticket_records']+=1
        focus['vehicle_present']+=has_vehicle
        focus['all_join_fields_present']+=has_vehicle and has_route and has_stop
        bus=bus_token(vehicle) if has_vehicle else None
        unlinked=set()
        for offer in by_place_day.get((day,route_name,stop_name),()):
            if bus==offer.bus:
                continue
            delta=abs((local-datetime.fromtimestamp(offer.ready,IST)).total_seconds())
            kind='different_vehicle' if has_vehicle else 'missing_vehicle'
            for w in WINDOWS:
                if delta<=w:
                    offer.unlinked[w].add(kind)
                    unlinked.update((p,w,kind) for p in offer.policies)
        for p,w,kind in unlinked:
            results[p,w][kind]+=1
        candidates=by_bus_day.get((day,bus),()) if has_vehicle else ()
        affected=set()
        for offer in candidates:
            offer.bus_seen=True
            if not has_route or route_name!=offer.route:
                continue
            offer.route_seen=True
            if not has_stop or stop_name!=offer.stop:
                continue
            offer.stop_seen=True
            delta=(local-datetime.fromtimestamp(offer.ready,IST)).total_seconds()
            relation='before' if delta<0 else 'after' if delta>0 else 'at'
            for w in WINDOWS:
                if abs(delta)<=w:
                    offer.near[w].add(relation)
                    affected.update((policy,w) for policy in offer.policies)
        if affected:
            digest=hashlib.sha256(json.dumps(cells,ensure_ascii=False,separators=(',',':')).encode()).digest()
            for key in affected:
                results[key]['matching_csv_records']+=1
                contents[key].add(digest)
    return {
        'csv_records':count['csv_records'],'incorrect_field_count':count['incorrect_field_count'],
        'invalid_or_missing_created_at':count['invalid_or_missing_created_at'],
        'created_at_without_offset':count['created_at_without_offset'],
        'timezone_offsets':dict(tz),'distinct_nonmissing_raw_vehicle_values':len(registrations),
        'vehicle_rows_with_edge_whitespace':count['vehicle_rows_with_edge_whitespace'],
        'value_checks':{k:dict(v) for k,v in values.items()},
        'distinct_normalized_route_labels':len(routes),'distinct_normalized_start_names':len(stops),
        'fields':{f:{s:v[s] for s in ('present','blank','null_literal')} for f,v in columns.items()},
        'dates_in_delhi':dict(sorted(dates.items())),
    },{k:{'matching_csv_records':results[k]['matching_csv_records'],'distinct_nine_field_contents':len(v),
          'same_place_time_records_with_missing_vehicle':results[k]['missing_vehicle'],
          'same_place_time_records_with_different_vehicle':results[k]['different_vehicle']}
       for k,v in contents.items()}


def failure_group(offer,dates):
    if offer.reference_error:
        return offer.reference_error
    if offer.day not in dates:
        return 'ticket_date_absent'
    if not offer.bus_seen:
        return 'no_ticket_on_exact_vehicle_and_date'
    if not offer.route_seen:
        return 'vehicle_found_without_exact_route_on_date'
    if not offer.stop_seen:
        return 'vehicle_route_found_without_exact_start_name_on_date'
    if offer.near[60]:
        return 'exact_join_within_60s'
    if offer.near[300]:
        return 'exact_join_only_between_60s_and_300s'
    return 'exact_fields_found_only_outside_300s'


def summarize(offers,dates,records):
    out={}
    for p in POLICIES:
        cohort=[o for o in offers if p in o.policies]
        matched_by_window={}
        for w in WINDOWS:
            matched=[o for o in cohort if o.near[w]]
            matched_by_window[str(w)]={
                'distinct_offers':len(matched),**records[p,w],
                'offers_with_before_ready_record':sum('before' in o.near[w] for o in matched),
                'offers_with_exact_ready_record':sum('at' in o.near[w] for o in matched),
                'offers_with_after_ready_record':sum('after' in o.near[w] for o in matched),
                'offers_near_same_place_ticket_with_missing_vehicle':sum('missing_vehicle' in o.unlinked[w] for o in cohort),
                'offers_near_same_place_ticket_with_different_vehicle':sum('different_vehicle' in o.unlinked[w] for o in cohort),
                'offers_by_gps_outcome':dict(Counter(o.outcome for o in matched))}
        out[p]={'offered':len(cohort),'eligible_unique_route_platform':sum(not o.reference_error for o in cohort),
                'offered_on_distinct_dates':len({o.day for o in cohort}),
                'mutually_exclusive_offer_results':dict(Counter(failure_group(o,dates) for o in cohort)),
                'windows_seconds':matched_by_window}
    return out


def audit(root,partial=False):
    dates,offers,signature,shards=load_offers(root,partial)
    path=root/'October-Delhi.csv.zip'
    with zipfile.ZipFile(path) as archive:
        members=[f for f in archive.namelist() if not f.startswith('__MACOSX/') and f.lower().endswith('.csv')]
        if len(members)!=1:
            raise ValueError('Expected one actual ticket CSV')
        with archive.open(members[0]) as binary, io.TextIOWrapper(binary,encoding='utf-8-sig',newline='') as stream:
            health,records=inspect_stream(stream,offers)
    with path.open('rb') as f:
        digest=hashlib.file_digest(f,'sha256').hexdigest()
    return {'schema':1,'complete':tuple(dates)==MONTH,'router_dates':dates,'ticket_archive_sha256':digest,
            'month_protocol_signature':signature,'daily_router_shard_sha256':shards,
            'ticket_field_health':health,'policies':summarize(offers,health['dates_in_delhi'],records)}


def render_report(report):
    health=report['ticket_field_health']
    policies=report['policies']
    dates=health['dates_in_delhi']
    n=health['csv_records']
    vehicles=health['fields']['vehicle_number']['present']
    all_fields=sum(v['all_join_fields_present'] for v in dates.values())
    lines=[
        '# October ticket creation near a bus offer','',
        f"The journey router offered {policies['revised']['offered']:,} conditional boardings under the revised rule. "
        f"Of these, {policies['revised']['windows_seconds']['60']['distinct_offers']:,} had a ticket created on the same "
        f"vehicle, route and uniquely named boarding platform within one minute of the rider’s ready time. "
        f"The five-minute window matched {policies['revised']['windows_seconds']['300']['distinct_offers']:,}. "
        "Ticket creation can corroborate use of a bus and route around that time. The archive records neither when "
        "the doors closed nor whether the simulated rider boarded.",'',
        '## Ticket coverage','',
        f"The supplied ONDC export contains {n:,} rows. Valid offset-aware creation timestamps cover "
        f"{len(dates)} local calendar dates, from {min(dates)} through {max(dates)}. "
        f"An exact vehicle value is present on {vehicles:,} rows ({vehicles/n:.1%}). "
        f"{all_fields:,} dated rows contain all four match fields. The archive has no ticket identifier. "
        "Passenger counts and fares do not create additional ticket records.",'',
        '## Results','',
        '| Rule | Offers | Unique boarding name on route | Offers within 60 s | CSV rows within 60 s | Offers within 300 s | CSV rows within 300 s |',
        '| --- | ---: | ---: | ---: | ---: | ---: | ---: |',
    ]
    for p,v in policies.items():
        a,b=v['windows_seconds']['60'],v['windows_seconds']['300']
        lines.append(f"| {p.capitalize()} | {v['offered']:,} | {v['eligible_unique_route_platform']:,} | "
                     f"{a['distinct_offers']:,} | {a['matching_csv_records']:,} | "
                     f"{b['distinct_offers']:,} | {b['matching_csv_records']:,} |")
    revised=policies['revised']['windows_seconds']['60']
    if revised['distinct_offers']:
        outcomes=', '.join(f"{n} {label}" for label,n in sorted(revised['offers_by_gps_outcome'].items()))
        before=revised['offers_with_before_ready_record']
        lines.extend(['',f"Among the one-minute matches, {before} revised {'offer' if before==1 else 'offers'} "
                      f"had a ticket created before readiness and {revised['offers_with_after_ready_record']} "
                      f"after readiness. The independent GPS classifications were {outcomes}."])
    descriptions={
        'missing_official_route_or_platform':'Missing official route or platform',
        'ambiguous_boarding_name_on_route':'Multiple platform IDs share that name on the route',
        'ticket_date_absent':'No ticket records on that date',
        'no_ticket_on_exact_vehicle_and_date':'No exact vehicle registration on that date',
        'vehicle_found_without_exact_route_on_date':'Vehicle found on that date, route unmatched',
        'vehicle_route_found_without_exact_start_name_on_date':'Vehicle and route found, boarding name unmatched',
        'exact_fields_found_only_outside_300s':'All fields matched only outside five minutes',
        'exact_join_only_between_60s_and_300s':'All fields matched in five minutes, outside one minute',
        'exact_join_within_60s':'All fields matched within one minute'}
    lines.extend(['','Each offer appears once in the following breakdown.','',
                  '| Result | Baseline | Revised |','| --- | ---: | ---: |'])
    for reason,label in descriptions.items():
        a,b=(policies[p]['mutually_exclusive_offer_results'].get(reason,0) for p in POLICIES)
        lines.append(f'| {label} | {a:,} | {b:,} |')
    lines.extend(['','## Interpretation','',
        'The match uses the raw vehicle registration exactly as supplied. Route and boarding names allow only differences '
        'in case and whitespace. Across all official patterns sharing the route label, the boarding name must resolve '
        'to a single platform ID. The creation timestamp must fall on the same Delhi service date. Both time windows include their '
        'endpoints and retain subsecond precision. The GPS outcome has no role in selecting a ticket.','',
        'Each CSV record counts once per policy and window, even if it could match multiple offers. The baseline and '
        'revised sets overlap. The JSON also counts distinct nine-field contents, offers with records created before '
        'or after readiness, and unlinked nearby tickets with missing or different vehicles. Identical contents may '
        'describe a duplicate export or separate transactions. '
        'A missing record can reflect a blank registration, a naming difference or use of another fare channel. '
        'These counts cannot establish a boarding success rate.','',
        'The full field-health table, daily record counts, matching totals and input checksums accompany this report. '
        '[Machine-readable results](../data/month-ticket-check.json) · [Frozen GPS study inputs](../data/month-boarding/manifest.json)',''])
    return '\n'.join(lines)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--preview',action='store_true',help='Inspect all ticket rows with completed days only; print a preview.')
    args=parser.parse_args()
    report=audit(ROOT,partial=args.preview)
    if args.preview:
        print(json.dumps({k:v for k,v in report.items() if k not in ('daily_router_shard_sha256',)},indent=2))
        return
    if not report['complete']:
        raise ValueError('The final cross-check requires all 31 daily router reports')
    out=ROOT/'data'/'month-ticket-check.json'
    tmp=out.with_suffix('.pending')
    tmp.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    tmp.replace(out)
    doc=ROOT/'docs'/'month-ticket-check.md'
    tmp=doc.with_suffix('.pending')
    tmp.write_text(render_report(report),encoding='utf-8')
    tmp.replace(doc)
    print(json.dumps(report['policies'],indent=2))


if __name__=='__main__':
    main()
