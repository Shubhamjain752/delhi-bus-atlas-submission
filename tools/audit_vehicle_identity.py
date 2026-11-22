"""Replay duplicate-assignment decisions and score later, independently observed GPS entries."""

import argparse
from collections import Counter, defaultdict
from datetime import datetime
import gzip
import hashlib
import importlib.util
from itertools import combinations
import json
from pathlib import Path
import statistics
import sys
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from transit.live import LiveOverlay, protobuf
from transit.router import IST, Router, approximate_m
from tools.verify_sample_passages import inspect

EXCLUDED = {'1459_FULL_00_05', '1996_FULL_00_35', '2106_FULL_00_40'}
import os
DIAGNOSTIC_VEHICLES = set(filter(None, os.environ.get("DELHI_AUDIT_EXCLUDED_VEHICLES", "").split(",")))
BASELINE_ZIP = '6612560336d244a12ca63392d6f03187808df5728d55870c6e3cdd1f3f753dea'
BASELINE_LIVE = '76a83eb7cbeaba690de229e860dcb9e885f37567389ff36688d9b762aea0d3fa'
HORIZON = 300


def frozen_overlay(source):
    if source.suffix.lower() == '.zip':
        if hashlib.sha256(source.read_bytes()).hexdigest() != BASELINE_ZIP:
            raise ValueError('The supplied review ZIP differs from the original baseline.')
        with zipfile.ZipFile(source) as archive:
            code = archive.read('Delhi Bus Atlas/transit/live.py')
    else:
        code = source.read_bytes()
    if hashlib.sha256(code).hexdigest() != BASELINE_LIVE:
        raise ValueError('The baseline live module differs from the frozen source.')
    spec = importlib.util.spec_from_loader('transit.identity_baseline', loader=None)
    module = importlib.util.module_from_spec(spec)
    exec(compile(code, 'frozen_transit_live.py', 'exec'), module.__dict__)
    return module.LiveOverlay


def static_match(desc, index):
    if not desc.trip_id or not desc.start_date or not desc.route_id or desc.schedule_relationship != 0:
        return None
    try:
        day = datetime.strptime(desc.start_date, '%Y%m%d').date()
    except ValueError:
        return None
    exact = index.ids.get(desc.trip_id)
    choices = [exact] if exact else index.by_clock.get((desc.route_id, index.clock(desc)), ())
    matches = [(pid, k) for pid, k in choices
               if index.router.patterns[pid].service in index.router._active(day)
               and index.router.patterns[pid].route == desc.route_id]
    if len(matches) != 1:
        return None
    pid, k = matches[0]
    return day, index.router.patterns[pid].trips[k]


def separated_vehicles(positions):
    """Require a separation beyond a generous allowance for asynchronously observed fixes."""
    evidence = defaultdict(list)
    for (vehicle, (ts, point)), (other, (other_ts, other_point)) in combinations(sorted(positions.items()), 2):
        distance = approximate_m(point, other_point)
        skew = abs(ts - other_ts)
        if distance < 250 + 35 * skew:
            continue
        common = {'distance_m': round(distance), 'timestamp_skew_seconds': skew}
        evidence[vehicle].append({'other_vehicle_id': other, **common})
        evidence[other].append({'other_vehicle_id': vehicle, **common})
    return evidence


def capture(folder):
    messages = []
    digest = hashlib.sha256()
    for path in sorted(folder.glob('*.pb.gz')):
        kind, receipt, _ = path.name.split('-')
        if kind not in ('positions', 'predictions'):
            continue
        raw = gzip.decompress(path.read_bytes())
        digest.update(path.name.encode() + b'\0' + raw)
        messages.append((int(receipt), kind, protobuf().FeedMessage.FromString(raw)))
    # Tied local receipt seconds mean both payloads had arrived; ingest positions first.
    return sorted(messages, key=lambda row: (row[0], row[1])), digest.hexdigest()


def observed_entries(folder, root):
    # These labels are deliberately calculated separately from the issuing loop.
    labels = defaultdict(dict)
    for window in inspect(folder, root)['candidates']:
        for event in window['events']:
            key = (window['service_date'], window['official_trip'], window['vehicle_id'], event['id'])
            labels[key][event['gps_time']] = event
    return labels


def audit(root, folder, original, saved_baseline, want_candidate):
    router = Router(root / 'delhi_buses_static_gtfs_v1.zip', root / 'data', None, None)
    index = LiveOverlay(None, root / 'data').bind(router)
    baseline = frozen_overlay(original)(None, root / 'data').bind(router) if original else None
    if not baseline and not saved_baseline:
        raise ValueError('Supply the original baseline or its previously recorded decisions.')
    if want_candidate and not hasattr(index, 'vehicle_runs'):
        raise RuntimeError('The candidate production vehicle_runs adapter is unavailable.')
    messages, digest = capture(folder)
    if saved_baseline and saved_baseline['capture_sha256'] != digest:
        raise ValueError('The baseline decision archive used a different capture.')
    if saved_baseline and (saved_baseline['zip_sha256'] != BASELINE_ZIP
                           or saved_baseline['live_py_sha256'] != BASELINE_LIVE):
        raise ValueError('Unknown baseline identity in the decision archive.')
    by_vehicle_pos = {}
    decisions = {}
    physical_pairs = set()
    duplicate_runs = set()
    confirmed_runs = set()
    for issue, kind, feed in messages:
        if not 0 <= issue - feed.header.timestamp <= 120:
            continue
        if kind == 'positions':
            if want_candidate:
                index.apply_positions(feed, issue, provider='community_positions')
            for ent in feed.entity:
                v = ent.vehicle
                if ent.is_deleted or not ent.HasField('vehicle') or not v.vehicle.id or not v.HasField('position'):
                    continue
                if not 0 <= issue - v.timestamp <= 120:
                    continue
                match = static_match(v.trip, index)
                previous = by_vehicle_pos.get(v.vehicle.id)
                if match and (not previous or previous[1] <= v.timestamp):
                    by_vehicle_pos[v.vehicle.id] = (match, v.timestamp, (v.position.latitude, v.position.longitude))
            continue
        if baseline:
            baseline.apply_trip_updates(feed, issue)
        if want_candidate:
            index.apply_trip_updates(feed, issue)
        groups = defaultdict(lambda: defaultdict(list))
        for ent in feed.entity:
            u = ent.trip_update
            if ent.is_deleted or not ent.HasField('trip_update') or not u.vehicle.id or u.vehicle.id in DIAGNOSTIC_VEHICLES:
                continue
            if not 0 <= issue - (u.timestamp or feed.header.timestamp) <= 120:
                continue
            match = static_match(u.trip, index)
            if match and match[1] not in EXCLUDED:
                groups[match][u.vehicle.id].append(u)
        with patch('time.time', return_value=issue):
            runs = index.vehicle_runs(issue) if want_candidate else {}
            for trip, vehicles in groups.items():
                if len(vehicles) < 2:
                    continue
                duplicate_runs.add(trip)
                locations = {}
                for vid, updates in vehicles.items():
                    pos = by_vehicle_pos.get(vid)
                    if len(updates) == 1 and pos and pos[0] == trip and 0 <= issue - pos[1] <= 120:
                        locations[vid] = pos[1:]
                evidence = separated_vehicles(locations)
                if not evidence:
                    continue
                confirmed_runs.add(trip)
                day, official = trip
                pid, k = index.ids[official]
                pattern = router.patterns[pid]
                midnight = int(datetime.combine(day, datetime.min.time(), tzinfo=IST).timestamp())
                for vehicle, siblings in evidence.items():
                    update = vehicles[vehicle][0]
                    named_runs = [r for r in runs.get(trip, ()) if r.vehicle_id == vehicle]
                    for call in update.stop_time_update:
                        stop = call.stop_id
                        if call.schedule_relationship != 0 or pattern.stops.count(stop) != 1:
                            continue
                        i = pattern.stops.index(stop)
                        if pattern.pickups[i] or (call.HasField('stop_sequence') and call.stop_sequence != pattern.sequences[i]):
                            continue
                        dwell = pattern.departures[i] - pattern.arrivals[i]
                        if call.HasField('departure') and call.departure.HasField('time'):
                            forecast = int(call.departure.time)
                        elif call.HasField('arrival') and call.arrival.HasField('time'):
                            forecast = int(call.arrival.time) + dwell
                        else:
                            continue
                        if not issue <= forecast <= issue + HORIZON:
                            continue
                        key = (day.strftime('%Y%m%d'), official, vehicle, stop)
                        physical_key = (day, vehicle, stop)
                        if physical_key in physical_pairs:
                            continue
                        physical_pairs.add(physical_key)
                        scheduled = midnight + pattern.starts[k] + pattern.departures[i]
                        decision_id = '/'.join(key)
                        if baseline:
                            original_live = baseline.departure(official, stop, day, scheduled, issue)
                        else:
                            recorded = saved_baseline['decisions'][decision_id]
                            if recorded[0] != issue:
                                raise ValueError('An issuance rule differed from the frozen replay.')
                            original_live = recorded[1]
                        named = named_runs[0].times.get(stop) if len(named_runs) == 1 else None
                        decisions[key] = {
                            'key': decision_id, 'route': router.routes[pattern.route][0], 'stop_name': router.stops[stop]['name'],
                            'issue': issue, 'raw_forecast': forecast, 'scheduled': scheduled, 'baseline_live': original_live,
                            'candidate_live': named, 'dwell_seconds': dwell, 'spatial_evidence': siblings,
                        }
    if saved_baseline and len(decisions) != len(saved_baseline['decisions']):
        raise ValueError('The recorded decision count differed from the baseline.')
    frozen = {'zip_sha256': BASELINE_ZIP, 'live_py_sha256': BASELINE_LIVE, 'capture_sha256': digest,
              'decisions': {v['key']: [v['issue'], v['baseline_live']] for v in decisions.values()}}
    labels = observed_entries(folder, root)
    for key, row in decisions.items():
        entries = labels.get(key, {})
        future = [e for e in entries.values() if row['issue'] < e['before_time'] < e['gps_time']]
        if len(future) == 1:
            event = future[0]
            row['observed'] = {field: event[field] for field in ('gps_time', 'gps_ist', 'before_time', 'offset_m')}
        else:
            row['observed'] = None
            if len(future) > 1:
                row['label_status'] = 'multiple later entries'
            elif entries:
                row['label_status'] = 'no entry strictly after issue'
            else:
                row['label_status'] = 'no qualifying GPS entry on the issued dated trip'
    last = messages[-1][0]
    return {
        'baseline_sha256': BASELINE_LIVE, 'capture_sha256': digest, 'forecast_horizon_seconds': HORIZON,
        'candidate_module_sha256': hashlib.sha256((root / 'transit' / 'live.py').read_bytes()).hexdigest() if want_candidate else None,
        'static_gtfs_sha256': hashlib.sha256((root / 'delhi_buses_static_gtfs_v1.zip').read_bytes()).hexdigest(),
        'excluded_trips': sorted(EXCLUDED), 'excluded_vehicles': sorted(DIAGNOSTIC_VEHICLES),
        'remaining_duplicated_trip_ids': len(duplicate_runs), 'spatially_corroborated_duplicated_trips': len(confirmed_runs),
        'first_receipt': messages[0][0], 'last_receipt': last, 'candidate_replayed': want_candidate,
        'summary': summary(list(decisions.values()), last, want_candidate), 'decisions': list(decisions.values()),
    }, frozen


def summary(rows, end, candidate):
    scored = [r for r in rows if r['observed']]
    variants = ['scheduled', 'baseline_live', 'raw_forecast']
    if candidate:
        variants.extend(['candidate_live', 'baseline_with_timetable', 'candidate_with_timetable'])

    def value(row, variant):
        if variant.endswith('_with_timetable'):
            live = row[variant.replace('_with_timetable', '_live')]
            return live if live is not None else row['scheduled']
        return row[variant]

    def metrics(records, variant):
        errors, bounds_errors, leads, missed = [], [], [], 0
        by_vehicle = defaultdict(list)
        for r in records:
            pred = value(r, variant)
            if pred is None:
                continue
            target = r['observed']
            errors.append(pred - target['gps_time'])
            by_vehicle[r['key'].split('/')[2]].append(abs(errors[-1]))
            bounds_errors.append(max(0, target['before_time'] - pred, pred - target['gps_time']))
            leads.append(target['gps_time'] - r['issue'])
            missed += pred < r['issue']
        if not errors:
            return {'scored': 0}
        return {'scored': len(errors), 'mae_seconds': round(statistics.mean(map(abs, errors)), 2),
                'median_abs_error_seconds': statistics.median(map(abs, errors)),
                'bias_seconds': round(statistics.mean(errors), 2),
                'mae_to_entry_interval_seconds': round(statistics.mean(bounds_errors), 2),
                'equal_vehicle_weight_mae_seconds': round(statistics.mean(statistics.mean(v) for v in by_vehicle.values()), 2),
                'within_entry_interval': bounds_errors.count(0), 'already_past_at_issue': missed,
                'median_actual_lead_seconds': statistics.median(leads)}

    keys = [r['key'].split('/') for r in scored]
    return {'issued_vehicle_platforms': len(rows), 'with_eligible_passage': len(scored),
            'unlabelled_counts': dict(Counter(r['label_status'] for r in rows if not r['observed'])),
            'five_minutes_of_capture_remaining': sum(r['issue'] + HORIZON <= end for r in rows),
            'with_eligible_passage_and_five_minutes_remaining': sum(r['issue'] + HORIZON <= end for r in scored),
            'labelled_vehicles': len({k[2] for k in keys}), 'labelled_dated_trips': len({tuple(k[:2]) for k in keys}),
            'labelled_routes': sorted({r['route'] for r in scored}),
            'all_issued_forecast_counts': {v: sum(value(r, v) is not None for r in rows) for v in variants},
            'all_labelled': {v: metrics(scored, v) for v in variants},
            'candidate_covered_matched': {v: metrics([r for r in scored if r['candidate_live'] is not None], v)
                                         for v in variants} if candidate else {}}


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--folder', type=Path, default=ROOT / 'data' / 'nov21-samples')
    cli.add_argument('--baseline-source', type=Path, help='Original review ZIP or its original transit/live.py')
    cli.add_argument('--baseline-decisions', type=Path, default=ROOT / 'data' / 'vehicle-identity-baseline.json')
    cli.add_argument('--raw-only', action='store_true', help='Omit the production candidate until it is built')
    cli.add_argument('--output', type=Path, default=ROOT / 'data' / 'vehicle-identity-validation.json')
    args = cli.parse_args()
    saved = None if args.baseline_source else json.loads(args.baseline_decisions.read_text())
    results, baseline = audit(ROOT, args.folder, args.baseline_source, saved, not args.raw_only)
    if args.baseline_source:
        args.baseline_decisions.write_text(json.dumps(baseline, indent=2), encoding='utf-8')
    args.output.write_text(json.dumps(results, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in results.items() if k != 'decisions'}, indent=2))


if __name__ == '__main__':
    main()
