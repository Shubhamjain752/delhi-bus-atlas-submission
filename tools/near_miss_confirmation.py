"""Build the frozen 29 October rider cohort and summarize a paired production-router replay."""

import argparse
import gzip
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.near_miss_peak import LABELS, PEAKS, PeakStudy, count_labels

DAY = '2026-10-29'
OPPORTUNITIES = ROOT/'data'/'near-miss-confirmation-opportunities.json.gz'
OUTPUT = ROOT/'data'/'near-miss-confirmation.json'
FROZEN_FILES = (
    'transit/router.py', 'transit/live.py', 'transit/replay.py', 'tools/replay_peak_router.py',
    'tools/near_miss_peak.py', 'data/eta_model.json.gz', 'delhi_buses_static_gtfs_v1.zip',
)


def frozen_inputs(opportunities):
    hashes = {name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in FROZEN_FILES}
    hashes['opportunities'] = hashlib.sha256(opportunities.read_bytes()).hexdigest()
    return hashes


def prepare(directory, output):
    db = directory/'bus_movements_2026_10_29.db'
    if not db.is_file():
        raise FileNotFoundError(db)
    study = PeakStudy(ROOT)
    primary = json.loads((ROOT/'data'/'near-miss-peak.json').read_text())
    if study.routes != primary['routes'] or study.model.metadata['train'] != primary['training_dates']:
        raise ValueError('The primary route cohort and model training dates must be preserved.')
    for path, key in [(study.gtfs,'gtfs_sha256'),(study.data/'eta_model.json.gz','fitted_model_sha256')]:
        if hashlib.sha256(path.read_bytes()).hexdigest() != primary[key]:
            raise ValueError('The static feed or fitted model differs from the primary experiment.')
    rows = []
    for peak in PEAKS:
        observations,_ = study.scan(db,DAY,peak)
        rows.extend(observations)
        print(f'Prepared {peak} inputs.',flush=True)
    rows.sort(key=lambda r:(r['ready'],r['bus']))
    if len({r['bus'] for r in rows}) != len(rows):
        raise ValueError('Multiple rider decisions for a physical bus on the confirmation date.')
    with gzip.GzipFile(filename=str(output),mode='wb',mtime=0) as dst:
        dst.write(json.dumps(rows,separators=(',',':')).encode())


def policies(rows):
    baseline = [r for r in rows if r.get('baseline_conditional_emitted')]
    revised = [r for r in rows if r.get('conditional_emitted')]
    if any(not r.get('baseline_conditional_emitted') for r in revised):
        raise ValueError('The exit veto introduced an offer absent in the paired baseline.')
    suppressed = [r for r in baseline if not r.get('conditional_emitted')]
    def counts(group):
        return {'offered':len(group),**count_labels(group)}
    return {'baseline':counts(baseline),'revised':counts(revised),'suppressed':counts(suppressed)}


def summarize(opportunities, paired_path, manifest, output):
    frozen = json.loads(manifest.read_text())
    if frozen != frozen_inputs(opportunities):
        raise ValueError('The frozen policy, model or cohort changed during the confirmation.')
    with gzip.open(opportunities,'rt',encoding='utf-8') as src:
        cohort = json.load(src)
    paired = json.loads(paired_path.read_text())
    rows = paired['rows']
    if paired['opportunities_sha256'] != frozen['opportunities']:
        raise ValueError('Paired replay and outcome report used different inputs.')
    eligible = {r['run']:r for r in cohort if 1 <= r['gap'] <= 30}
    if len(rows) != len(eligible) or {r['run'] for r in rows} != set(eligible):
        raise ValueError('The paired replay omitted an eligible opportunity or repeated one.')
    for row in rows:
        original = eligible[row['run']]
        if any(row[k] != original[k] for k in ('day','peak','bus','ready','gap','outcome')):
            raise ValueError('Router row identity or external GPS outcome changed.')
    router = policies(rows)
    for key,sources in [('baseline',paired['baseline']['overall']),('revised',paired['overall'])]:
        expected = {'offered':sources['conditional_emitted'],**sources['emitted_outcomes']}
        if router[key] != expected:
            raise ValueError('Stored offer labels disagree with the paired router summary.')
    router['by_peak'] = {p:policies([r for r in rows if r['peak']==p]) for p in PEAKS}
    counts = paired['overall']
    report = {
        'schema':1, 'date':DAY, 'windows_ist':{'AM':['08:00','09:30'],'PM':['17:00','18:30']},
        'opportunities':len(cohort), 'positive_gap_1_30':len(rows),
        'all_attempted_outcomes':count_labels(rows),
        'by_peak':{p:{'opportunities':sum(r['peak']==p for r in cohort),
                       'positive_gap_1_30':sum(r['peak']==p for r in rows)} for p in PEAKS},
        'scope':{'routes_with_an_opportunity':len({r['route_id'] for r in cohort}),
                 'routes_attempted':counts['attempted_routes'],
                 'routes_replayed':counts['replayed_routes'],
                 'matched_and_replayed':counts['statically_matched_and_replayed'],
                 'accepted_reconstructed_forecasts':counts['accepted_live_call_at_issue'],
                 'exclusions':counts['exclusions']},
        'router':router, 'gone_offer_receipt_audit':paired.get('gone_offer_receipt_audit',[]),
        'input_hashes':frozen, 'router_replay_sha256':hashlib.sha256(paired_path.read_bytes()).hexdigest(),
        'offers':[{**r,'route':eligible[r['run']]['route'],'gps_entry_exit':eligible[r['run']]['gps_entry_exit'],
                   'forecast':eligible[r['run']]['forecast'],'forecast_receipt':eligible[r['run']]['forecast_receipt']}
                  for r in rows if r.get('baseline_conditional_emitted') or r.get('conditional_emitted')],
    }
    output.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps({k:report[k] for k in ('date','opportunities','positive_gap_1_30','scope','router')}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    cmd = parser.add_subparsers(dest='command', required=True)
    cohort = cmd.add_parser('prepare')
    cohort.add_argument('directory',type=Path)
    cohort.add_argument('--opportunities',type=Path,default=OPPORTUNITIES)
    freeze = cmd.add_parser('freeze',help='Record forecast, policy and input hashes before inspecting outcomes')
    freeze.add_argument('manifest',type=Path)
    freeze.add_argument('--opportunities',type=Path,default=OPPORTUNITIES)
    summary = cmd.add_parser('summarize')
    summary.add_argument('manifest',type=Path)
    summary.add_argument('--opportunities',type=Path,default=OPPORTUNITIES)
    summary.add_argument('--paired',type=Path,default=ROOT/'data'/'near-miss-router-confirmation.json')
    summary.add_argument('--output',type=Path,default=OUTPUT)
    args = parser.parse_args()
    if args.command == 'prepare':
        prepare(args.directory,args.opportunities)
    elif args.command == 'freeze':
        args.manifest.write_text(json.dumps(frozen_inputs(args.opportunities),indent=2),encoding='utf-8')
    elif args.command == 'summarize':
        summarize(args.opportunities,args.paired,args.manifest,args.output)


if __name__ == '__main__':
    main()
