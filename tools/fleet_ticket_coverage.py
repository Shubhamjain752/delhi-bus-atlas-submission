"""Stream the supplied October tickets and measure exact route-label representation."""
import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import io
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DAYS = tuple(f'2026-10-{d:02d}' for d in range(1,32))
IST = timezone(timedelta(hours=5,minutes=30))
FIELDS = ('passenger_count','amount','vehicle_number','ticket_type_id','end_stop_name',
          'route','start_stop_name','created_at','is_ac')
CATEGORIES = ('named_boarding','short_link_only','neither_study','ambiguous_coverage',
              'unmapped_label','missing_label','unusable_timestamp','outside_october','malformed_record')
UNITS = ('ticket_records','recorded_passengers','missing_passenger_count_records','invalid_passenger_count_records')


def digest(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f,'sha256').hexdigest()


def normalized(value):
    return ' '.join(value.casefold().split())


def missing(value):
    return not value.strip() or value.strip().upper()=='NULL'


def gtfs_names(path):
    names = defaultdict(list)
    ids = set()
    with zipfile.ZipFile(path) as z, z.open('routes.txt') as f:
        for r in csv.DictReader(io.TextIOWrapper(f,encoding='utf-8-sig',newline='')):
            rid,agency = r['route_id'],r['agency_id']
            label = (r['route_short_name'] or r['route_long_name']).strip()
            if rid in ids or not rid or not agency or not label:
                raise ValueError('Invalid or duplicate GTFS route ID, missing route name or missing agency.')
            ids.add(rid)
            names[normalized(label)].append((rid,agency,label))
    return names,ids


def load_evidence(data, ids):
    manifest = json.loads((data/'fleet-cases/manifest.json').read_text())
    audit_path = data/'fleet-receipt-audit.json'
    audit = json.loads(audit_path.read_text())
    if audit['signature']!=manifest['signature'] or set(audit['days'])!=set(DAYS):
        raise ValueError('Incomplete or mismatched boarding case audit.')
    named = set()
    signatures = set()
    hashes = {}
    named_count = 0
    for day in DAYS:
        f = data/'fleet-platform'/f'{day}.json.gz'
        with gzip.open(f,'rt',encoding='utf-8') as packed:
            j = json.load(packed)
        if j['day']!=day or len(j['rows'])!=audit['days'][day]['eligible']:
            raise ValueError('Dated named checks differ from the independent fleet audit: '+day)
        signatures.add(j['signature'])
        hashes[day]=digest(f)
        named.update(str(row[0]) for row in j['rows'])
        named_count += len(j['rows'])
    if len(signatures)!=1 or named_count!=audit['counts']['eligible'] or not named<=ids:
        raise ValueError('Incompatible named boarding route records.')
    short_path = data/'fleet-short-links.json'
    short_report = json.loads(short_path.read_text())
    if (not short_report['complete'] or set(short_report['days'])!=set(DAYS) or
            short_report['input_archive_sha256']!=manifest['source_archive_sha256']):
        raise ValueError('Incomplete short-link evidence or unexpected source archive.')
    short = {r for r,v in short_report['routes'].items() if v['issued']>0}
    if not short<=ids:
        raise ValueError('Short-link evidence includes an unknown official route.')
    return named,short,{'boarding_case_signature':manifest['signature'],'platform_signature':signatures.pop(),
                        'expected_static_sha256':manifest['source_sha256']['delhi_buses_static_gtfs_v1.zip'],
                        'platform_day_sha256':hashes,'case_audit_sha256':digest(audit_path),
                        'short_link_sha256':digest(short_path),'named_platform_checks':named_count}


def classify(key, names, named, short):
    if not key:
        return 'missing_label','missing_label'
    candidates = names.get(key,())
    if not candidates:
        return 'unmapped_label','unmapped_label'
    categories = {('named_boarding' if rid in named else 'short_link_only' if rid in short else 'neither_study')
                  for rid,_,_ in candidates}
    agencies = {agency for _,agency,_ in candidates}
    category = categories.pop() if len(categories)==1 else 'ambiguous_coverage'
    agency = agencies.pop() if len(agencies)==1 else 'ambiguous_agency'
    return category,agency


def add_record(counts, passenger):
    counts['ticket_records']+=1
    if missing(passenger):
        counts['missing_passenger_count_records']+=1
    elif passenger.isascii() and passenger.isdecimal() and int(passenger)>0:
        counts['recorded_passengers']+=int(passenger)
    else:
        counts['invalid_passenger_count_records']+=1


def stream_tickets(path):
    by_label,excluded,dates=defaultdict(Counter),defaultdict(Counter),Counter()
    display = {}
    with zipfile.ZipFile(path) as z:
        files=[i for i in z.infolist() if not i.filename.startswith('__MACOSX/') and i.filename.lower().endswith('.csv')]
        if len(files)!=1:
            raise ValueError('Expected exactly one non-metadata ticket CSV.')
        with z.open(files[0]) as binary,io.TextIOWrapper(binary,encoding='utf-8-sig',newline='') as src:
            rows=csv.reader(src)
            if tuple(next(rows))!=FIELDS:
                raise ValueError('Unexpected ONDC ticket schema.')
            for row in rows:
                if len(row)!=len(FIELDS):
                    add_record(excluded['malformed_record'],'')
                    continue
                passenger,label,created=row[0],row[5],row[7]
                try:
                    stamp=datetime.fromisoformat(created)
                except ValueError:
                    stamp=None
                if stamp is None or stamp.utcoffset() is None:
                    add_record(excluded['unusable_timestamp'],passenger)
                    continue
                day=stamp.astimezone(IST).date().isoformat()
                dates[day]+=1
                if not '2026-10-01'<=day<='2026-10-31':
                    add_record(excluded['outside_october'],passenger)
                    continue
                key='' if missing(label) else normalized(label)
                add_record(by_label[key],passenger)
                spelling=' '.join(label.split())
                if key and (key not in display or spelling<display[key]):
                    display[key]=spelling
    return by_label,display,excluded,dates,files[0].filename


def complete(counts):
    return {u:counts[u] for u in UNITS}


def run(root):
    data=root/'data'
    ticket=root/'October-Delhi.csv.zip'
    gtfs=root/'delhi_buses_static_gtfs_v1.zip'
    names,ids=gtfs_names(gtfs)
    named,short,study=load_evidence(data,ids)
    static_checksum=digest(gtfs)
    if study['expected_static_sha256']!=static_checksum:
        raise ValueError('The official route lookup differs from the timetable used in the boarding study.')
    checksum=digest(ticket)
    old=json.loads((data/'month-ticket-check.json').read_text())
    if not old['complete'] or old['ticket_archive_sha256']!=checksum:
        raise ValueError('Ticket archive differs from the earlier independent field audit.')
    by_label,display,excluded,dates,member=stream_tickets(ticket)
    if dict(dates)!={d:v['dated_ticket_records'] for d,v in old['ticket_field_health']['dates_in_delhi'].items()}:
        raise ValueError('Local-date record counts fail independent ticket audit.')
    categories=defaultdict(Counter)
    agencies=defaultdict(lambda:defaultdict(Counter))
    official_multi=defaultdict(Counter)
    labels=[]
    for key,c in by_label.items():
        category,agency=classify(key,names,named,short)
        matches=sorted(names.get(key,()))
        categories[category].update(c)
        agencies[agency][category].update(c)
        if len(matches)>1:
            official_multi[category].update(c)
        if not key:
            continue
        labels.append({'ticket_label':display[key],'normalized_label':key,'category':category,
                       'gtfs_agencies':sorted({m[1] for m in matches}),
                       'official_route_ids':[m[0] for m in matches],**complete(c)})
    for group,c in excluded.items():
        categories[group].update(c)
        agencies['record_outside_mapping'][group].update(c)
    total=sum(categories.values(),Counter())
    old_health=old['ticket_field_health']
    if (total['ticket_records']!=old_health['csv_records'] or
            total['invalid_passenger_count_records']+total['missing_passenger_count_records']!=
            old_health['value_checks']['passenger_count'].get('other',0)+old_health['incorrect_field_count']):
        raise ValueError('Ticket or passenger field counts differ from independent field audit.')
    labels.sort(key=lambda row:(-row['recorded_passengers'],-row['ticket_records'],row['normalized_label']))
    by_group={k:complete(categories[k]) for k in CATEGORIES}
    fractions={}
    for k,c in by_group.items():
        fractions[k]={unit:(c[unit]/total[unit] if total[unit] else None)
                      for unit in ('ticket_records','recorded_passengers')}
    return {'schema':1,'scope':'ONDC October 2026 Delhi export, civil dates in Asia/Kolkata',
            'ticket_archive':ticket.name,'csv_member':member,'ticket_archive_sha256':checksum,
            'official_gtfs_sha256':static_checksum,'study_inputs':study,'totals':complete(total),
            'official_route_ids':len(ids),'official_normalized_labels':len(names),
            'official_labels_with_multiple_ids':sum(len(v)>1 for v in names.values()),
            'ticket_labels_with_multiple_official_ids':sum(len(r['official_route_ids'])>1 for r in labels),
            'named_route_ids':len(named),'short_link_issued_route_ids':len(short),
            'short_only_route_ids':len(short-named),'ids_in_both_studies':len(named&short),
            'ticket_route_labels':len(labels),'category_counts':by_group,'fraction_of_export':fractions,
            'distinct_ticket_labels_by_category':dict(Counter(r['category'] for r in labels)),
            'ticket_records_by_local_date':dict(sorted(dates.items())),
            'official_agency_cross_tab':{a:{k:complete(c) for k,c in sorted(groups.items())}
                                         for a,groups in sorted(agencies.items())},
            'multiple_official_id_records_by_category':{k:complete(c) for k,c in sorted(official_multi.items())},
            'labels_ranked_by_recorded_passengers':labels}


def percent(value,total):
    if not total:
        return 'n/a'
    pct=100*value/total
    return f'{pct:.4f}%' if 0<pct<0.01 else f'{pct:.2f}%'


def markdown(j):
    total=j['totals']
    labels=j['labels_ranked_by_recorded_passengers']
    names={'named_boarding':'Named boarding study','short_link_only':'Short-link study only',
           'neither_study':'Official route in neither study','ambiguous_coverage':'Official IDs disagree on coverage',
           'unmapped_label':'No exact official label','missing_label':'Missing ticket route',
           'unusable_timestamp':'Unusable creation time','outside_october':'Outside October','malformed_record':'Malformed CSV row'}
    table=['| Route-label result | Ticket records | Share | Recorded passengers | Share |',
           '|---|---:|---:|---:|---:|']
    for k,c in j['category_counts'].items():
        if k in ('unusable_timestamp','outside_october','malformed_record') and not c['ticket_records']:
            continue
        table.append(f"| {names[k]} | {c['ticket_records']:,} | {percent(c['ticket_records'],total['ticket_records'])} | "
                     f"{c['recorded_passengers']:,} | {percent(c['recorded_passengers'],total['recorded_passengers'])} |")
    table.append(f"| **Total** | **{total['ticket_records']:,}** | **100%** | **{total['recorded_passengers']:,}** | **100%** |")
    quality=('Every record has a positive integer passenger count.' if not
             (total['missing_passenger_count_records'] or total['invalid_passenger_count_records']) else
             f"{total['missing_passenger_count_records']:,} counts are missing and "
             f"{total['invalid_passenger_count_records']:,} are invalid. Those rows contribute unknown passenger volume.")
    populations=j['distinct_ticket_labels_by_category']
    lines=['# Which October ticket routes have GPS evidence?', '',
           f"Routes represented in the named boarding study account for "
           f"{percent(j['category_counts']['named_boarding']['recorded_passengers'],total['recorded_passengers'])} "
           "of the passenger counts recorded in this ONDC export. Each ticket record contributes once. "
           "The passenger column sums its positive integer count. "
           'A route qualifies for named boarding if at least one uniquely dated GPS case passed the identity and timetable checks '
           'anywhere in October. A short-link route qualifies after an actual two- or three-stop forecast was issued.', '',
           *table,'',f"Of {j['ticket_route_labels']:,} distinct nonmissing ticket labels, "
           f"{populations.get('named_boarding',0):,} have named boarding evidence, "
           f"{populations.get('short_link_only',0):,} have short-link evidence only, "
           f"{populations.get('neither_study',0):,} match official routes in neither study and "
           f"{populations.get('unmapped_label',0):,} have no official match. "+quality,'',
           '## Operator follow-up','',
           'Labels below are ranked within each group by recorded passengers, with ticket records as the tie-breaker. '
           'The first group can be investigated against a known timetable route. The second needs an operator crosswalk '
           'before a GPS coverage claim can be made. The full ranking is available in the machine-readable result.','']
    for title,allowed in [('Official route in neither study',{'neither_study'}),
                          ('Missing or ambiguous official match',{'unmapped_label','ambiguous_coverage'})]:
        selection=[r for r in labels if r['category'] in allowed][:10]
        lines += ['### '+title,'']
        if not selection:
            lines+=['No ticket route labels in this group.','']
            continue
        lines+=['| Ticket route | Official agency | Route IDs | Ticket records | Recorded passengers |',
                '|---|---|---|---:|---:|']
        for r in selection:
            name=r['ticket_label'].replace('|','\\|')
            lines.append(f"| {name} | {', '.join(r['gtfs_agencies']) or 'Unresolved'} | "
                         f"{', '.join(r['official_route_ids']) or '—'} | {r['ticket_records']:,} | {r['recorded_passengers']:,} |")
        lines+=['']
    lines+=['## How to interpret the match','',
            'The CSV has no agency or route-ID column. The lookup uses the official route_short_name, with route_long_name '
            'as a fallback when the short name is empty. It preserves direction, punctuation, internal characters and '
            'numeric prefixes. If a normalized label has multiple official IDs, it receives a coverage category only '
            'when every candidate ID agrees. The agency cross-tab uses the same unanimity rule. '
            f"This timetable has {j['official_labels_with_multiple_ids']:,} normalized labels shared by multiple IDs.",'',
            "The GPS archive is described as DTC-only. The official timetable assigns matched labels to both DTC and DIMTS. "
            "The agency shown here belongs to the timetable route. The ticket export cannot independently confirm the operator "
            "of a particular bus.",'',
            'These shares describe demand recorded through this ticket export. A match establishes route-name representation '
            'at some point during the month. It cannot establish whether the ticketed bus was visible at that passenger’s '
            'time or stop, whether the forecast was correct, or whether anyone caught a bus. Cash and other fare channels '
            'are outside this export. The missing ticket identifier also prevents identifying unique transactions or people.', '',
            'Reproduce with `python tools/fleet_ticket_coverage.py` from the project root. '
            '[Full aggregate results](../data/fleet-ticket-coverage.json) · [Ticket field audit](month-ticket-check.md) · '
            '[Short-route evidence](fleet-short-links.md)','']
    return '\n'.join(lines)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=ROOT)
    args=parser.parse_args()
    result=run(args.root)
    for target,contents in [(args.root/'data/fleet-ticket-coverage.json',json.dumps(result,ensure_ascii=False,indent=2)+'\n'),
                            (args.root/'docs/fleet-ticket-coverage.md',markdown(result))]:
        pending=target.with_suffix('.pending')
        pending.write_text(contents,encoding='utf-8')
        pending.replace(target)
    print(result['totals'],result['category_counts'])
