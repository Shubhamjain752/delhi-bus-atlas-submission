"""Score independent October peak-hour platform readiness decisions from archived GPS."""

import argparse
from bisect import bisect_left
from collections import Counter, defaultdict
from datetime import date, datetime, time
from functools import lru_cache
import gzip
import hashlib
import json
from pathlib import Path
import re
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.train_eta import boundary, clock, format_clock, gtfs_routes, open_db
from transit.eta import EtaModel, IST, read_data
from transit.router import Router

DAYS = ('2026-10-22', '2026-10-23', '2026-10-26')
PEAKS = {'AM': (8*3600, 9*3600+1800), 'PM': (17*3600, 18*3600+1800)}
GRACES = (0, 5, 15, 30, 60)
LABELS = ('approaching', 'gone', 'uncertain', 'unverified')


def token(*parts, size=8):
    return hashlib.blake2s('|'.join(map(str, parts)).encode(), digest_size=size).hexdigest()


def ready_clock(stop, prediction):
    phase = int(token(stop, 'platform-grid'), 16) % 120
    return ((prediction - phase + 60) // 120) * 120 + phase


def nominal_start_minute(route, trip):
    """The archive appends an internal suffix to route_HH_MM identifiers."""
    m = re.fullmatch(re.escape(route)+r'_(\d{1,2})_(\d{2})(?:_\w+)?', trip)
    return int(m[1])*60+int(m[2]) if m and int(m[2]) < 60 else None


def chronological_indices(observed, receipts):
    """Never insert a late, older observation into an already issued trajectory."""
    order = np.lexsort((observed,receipts))
    timestamps = observed[order]
    previous = np.r_[-1,np.maximum.accumulate(timestamps[:-1])]
    return order[timestamps > previous]


def select_routes(model, router):
    counts = Counter()
    for pair, (overall, _) in model.pairs.items():
        route = pair.split('|')[0]
        if route in router.routes and not re.search(r'(?<!\d)0?502(?!\d)', router.routes[route][0]):
            counts[route] += overall[0]
    return sorted(counts, key=lambda route: (-counts[route], route))[:40]


def read_window(path, routes, start, end, midnight):
    binds = ','.join('?' for _ in routes)
    lo, hi = format_clock(start - 600), format_clock(end + 600)
    with open_db(path) as db:
        first, last = boundary(db, lo), boundary(db, hi)
        query = f"""SELECT route_id AS route, vehicle_id AS vehicle, trip_id AS trip,
                           CAST(lat AS REAL) AS lat, CAST(lng AS REAL) AS lon,
                           CAST(timestamp AS INT) AS observed, time AS archive_time
                    FROM vehicle_feed WHERE rowid >= ? AND rowid < ? AND route_id IN ({binds})
                    AND time >= ? AND time < ? AND vehicle_id IS NOT NULL AND trip_id IS NOT NULL"""
        frame = pd.read_sql_query(query, db, params=[first, last, *map(int, routes), lo, hi])
    raw_count = len(frame)
    clocks = {value: midnight + clock(value) for value in frame.archive_time.unique()}
    frame['receipt'] = frame.archive_time.map(clocks)
    frame = frame[frame.lat.between(28, 30) & frame.lon.between(76, 78)
                  & (frame.receipt-frame.observed).between(0, 90)].copy()
    frame = frame.sort_values(['route', 'vehicle', 'trip', 'observed', 'receipt']).drop_duplicates(
        ['route', 'vehicle', 'trip', 'observed'])
    return frame, raw_count


def target_outcome(xy, observed, accepted, broken, i, target, points, ready):
    """Only outcome labels see fixes after readiness. Broken or reversed traces are unscored."""
    stop = points[target]
    entered = None
    entry_before = None
    for j in range(i+1, len(xy)):
        if broken[j] or observed[j] - observed[i] > 720:
            break
        distance = np.linalg.norm(xy[j] - stop)
        if entered is None:
            idx = accepted[j]
            if idx >= 0 and (idx < target-1 or idx > target):
                break
            if idx == target and np.linalg.norm(xy[j-1]-stop) > 45:
                entered, entry_before = int(observed[j]), int(observed[j-1])
        elif distance > 45:
            onward = points[target+1] - stop
            progress = np.dot(xy[j] - stop, onward) / np.linalg.norm(onward)
            if progress >= 20:
                exit_seen = int(observed[j])
                if ready <= entry_before:
                    return 'approaching', [entry_before, entered, exit_seen]
                if ready >= exit_seen:
                    return 'gone', [entry_before, entered, exit_seen]
                return 'uncertain', [entry_before, entered, exit_seen]
            break
    if entered is not None and ready <= entry_before:
        return 'approaching', [entry_before, entered, None]
    if entered is not None:
        return 'uncertain', [entry_before, entered, None]
    return 'unverified', None


def fresh_approach(xy, observed, receipt, broken, i, a, b, ready):
    """Two received fixes progressing along the upstream-to-platform chord as of readiness."""
    j = int(np.searchsorted(observed, ready, side='right'))
    known = np.flatnonzero((receipt[i:j] <= ready) & (observed[i:j] >= ready-90)) + i
    if len(known) < 2:
        return False
    u, v = known[-2:]
    if ready-observed[v] > 60 or np.any(broken[i+1:v+1]):
        return False
    chord = b-a
    length = np.linalg.norm(chord)
    unit = chord/length
    progress = np.dot(xy[v]-a, unit)
    offset = np.linalg.norm(xy[v]-(a+progress*unit))
    return bool(0 <= progress < length-45 and offset <= 75 and np.dot(xy[v]-xy[u], unit) >= 15)


class PeakStudy:
    windows = PEAKS

    def assigned_peak(self, day, vehicle):
        return ('AM','PM')[int(token(day,vehicle,'peak'),16) % 2]

    def __init__(self, root):
        self.data = root / 'data'
        self.gtfs = root / 'delhi_buses_static_gtfs_v1.zip'
        self.model = EtaModel(read_data(self.data / 'eta_model.json.gz'))
        self.router = Router(self.gtfs, self.data, None, None)
        self.routes = select_routes(self.model, self.router)
        self.patterns = gtfs_routes(self.gtfs, self.routes)

    @lru_cache(maxsize=None)
    def onward_trips(self, day, route, board, dest):
        active = self.router._active(day)
        rows = []
        for pid, i in self.router.at[board]:
            p = self.router.patterns[pid]
            if (p.service not in active or p.route != route or p.pickups[i] or i+1 >= len(p.stops)
                    or p.stops[i+1] != dest or p.drops[i+1]):
                continue
            rows.extend((t+p.departures[i], t+p.arrivals[i+1], trip, (t+p.departures[0])//60)
                        for t, trip in zip(p.starts, p.trips))
        return sorted(rows)

    def fallback(self, day, route, stop, dest, ready_s, ride_s, current_trip):
        rows = self.onward_trips(day, route, stop, dest)
        minute = nominal_start_minute(route, current_trip)
        if minute is None and not any(trip == current_trip for _,_,trip,_ in rows):
            return None
        lo = bisect_left(rows, (ready_s,))
        hi = bisect_left(rows, (ready_s+3600,))
        options = [arr for _, arr, trip, start in rows[lo:hi] if trip != current_trip and start != minute]
        return min(options)-ready_s-ride_s if options else None

    def scan(self, path, day_text, peak, include_model_source=False):
        day = date.fromisoformat(day_text)
        midnight = int(datetime.combine(day, time(), IST).timestamp())
        start, end = self.windows[peak]
        frame, raw = read_window(path, self.routes, start, end, midnight)
        output = {}
        assigned = set()
        for route_number, data in frame.groupby('route', sort=False):
            route = str(int(route_number))
            stops, nominal, counts, tree, scale = self.patterns[route]
            points = tree.data
            xy = data[['lat', 'lon']].to_numpy() * scale
            distances, indices = tree.query(xy, k=2)
            accepted = np.where((distances[:,0] <= 45) & (distances[:,1]-distances[:,0] >= 12),
                                indices[:,0], -1)
            data = data.assign(row=np.arange(len(data)))
            for (vehicle, trip), trace in data.groupby(['vehicle', 'trip'], sort=False):
                if self.assigned_peak(day_text, vehicle) != peak:
                    continue
                assigned.add(str(vehicle))
                original_ts, original_received = trace.observed.to_numpy(), trace.receipt.to_numpy()
                order = chronological_indices(original_ts,original_received)
                ix = trace.row.to_numpy()[order]
                ts, received = original_ts[order], original_received[order]
                loc, at = xy[ix], accepted[ix]
                # Two distinct geofence entries can first be known on report three.
                if len(ts) < 3:
                    continue
                gaps = np.diff(ts)
                jumps = np.linalg.norm(np.diff(loc, axis=0), axis=1)
                broken = np.r_[True, (gaps <= 0) | (gaps > 60) | (jumps > 35*gaps)]
                chunk = np.cumsum(broken)
                hits = np.flatnonzero((at[1:] >= 0) & (at[1:] != at[:-1]) & ~broken[1:]) + 1
                prev = None
                current_trip = str(trip)
                for i in hits:
                    idx = at[i]
                    old = prev
                    if counts[stops[idx]] == 1:
                        prev = i
                    if (old is None or chunk[old] != chunk[i] or not 1 <= idx-at[old] <= 3
                            or ts[i]-ts[old] > 600 or received[old] > received[i] or received[i-1] > received[i]
                            or not start <= received[i]-midnight < end or idx+2 >= len(stops)):
                        continue
                    if np.linalg.norm(loc[i-1]-points[idx]) <= 45:
                        continue
                    a, b, c = stops[idx:idx+3]
                    if any(counts[s] != 1 for s in (a,b,c)):
                        continue
                    if min(np.linalg.norm(points[idx+1]-points[idx]),np.linalg.norm(points[idx+2]-points[idx+1])) < 100:
                        continue
                    if not self.onward_trips(day, route, b, c):
                        continue
                    duration, model_source = self.model.duration(route,a,b,int(ts[i]-midnight),nominal[idx],
                                                     asof=int(received[i]), service_date=day)
                    if not 30 <= duration <= 240:
                        continue
                    predicted = int(ts[i])+duration
                    ready = ready_clock(b,predicted)
                    if not received[i] < ready or not start <= ready-midnight < end:
                        continue
                    rank = (int(received[i]), int(ts[i]), route, current_trip, b)
                    if str(vehicle) in output and output[str(vehicle)][0] <= rank:
                        continue
                    ride, _ = self.model.duration(route,b,c,ready-midnight,nominal[idx+1],asof=ready,service_date=day)
                    gain = self.fallback(day,route,b,c,ready-midnight,ride,current_trip)
                    gate = fresh_approach(loc,ts,received,broken,i,points[idx],points[idx+1],ready)
                    label, bracket = target_outcome(loc,ts,at,broken,i,idx+1,points,ready)
                    output[str(vehicle)] = (rank,{
                        'day':day_text, 'peak':peak, 'bus':token(vehicle,'bus'), 'run':token(day_text,vehicle,trip),
                        'route_id':route, 'route':self.router.routes[route][0], 'upstream':a, 'stop':b, 'next_stop':c,
                        'origin_gps':int(ts[i]), 'forecast_receipt':int(received[i]), 'forecast':predicted,
                        'ready':ready, 'gap':ready-predicted, 'fallback_gain_s':gain, 'fresh_approach':gate,
                        'outcome':label, 'gps_entry_exit':bracket,
                    })
                    if include_model_source:
                        output[str(vehicle)][1]['forecast_source'] = model_source
        return [v for _,v in output.values()], {
            'raw_rows':raw,'fresh_unique_pings':len(frame),'assigned_vehicles_seen':len(assigned),'opportunities':len(output)}


GATES = {'unconditional': lambda r: True,
         'fallback_5min': lambda r: r['fallback_gain_s'] is not None and r['fallback_gain_s'] >= 300,
         'fresh_approach': lambda r:r['fresh_approach'],
         'approach_and_fallback': lambda r:r['fresh_approach'] and r['fallback_gain_s'] is not None
                                                            and r['fallback_gain_s'] >= 300}


def count_labels(rows):
    c = Counter(r['outcome'] for r in rows)
    return {label: c[label] for label in LABELS}


def evaluate(rows):
    borderline = [r for r in rows if r['gap'] > 0]
    all_positive = sum(r['outcome']=='approaching' for r in borderline)
    result = {}
    for method, gate in GATES.items():
        result[method] = {}
        for cutoff in GRACES:
            offers = [r for r in borderline if r['gap'] <= cutoff and gate(r)]
            outcomes = count_labels(offers)
            recovered, false = outcomes['approaching'], outcomes['gone']
            classified = recovered + false
            result[method][str(cutoff)] = {
                'offered':len(offers), **outcomes,
                'declined_approaching':all_positive-recovered,
                'precision_among_definitive':round(recovered/classified,4) if classified else None,
                'minimum_supported_share_of_all_offers':round(recovered/len(offers),4) if offers else None,
                'confirmed_recovery_fraction':round(recovered/all_positive,4) if all_positive else None,
                'coverage_of_borderline':round(len(offers)/len(borderline),4) if borderline else None,
            }
    return {'opportunities':len(rows),'ordinary_clock_catchable':count_labels([r for r in rows if r['gap']<=0]),
            'borderline':count_labels(borderline),'thresholds':result}


def bands(rows):
    return {f'{lo}-{hi}':{gate:count_labels([r for r in rows if lo<=r['gap']<=hi and check(r)])
                           for gate,check in GATES.items()}
            for lo,hi in [(1,5),(6,15),(16,30),(31,60)]}


def run(args):
    study = PeakStudy(ROOT)
    rows, counts = [], {}
    for day in DAYS:
        path = args.directory / f"bus_movements_{day.replace('-','_')}.db"
        if not path.is_file():
            raise FileNotFoundError(path)
        for peak in PEAKS:
            checkpoint = args.cache_dir / f'{day}-{peak}.json' if args.cache_dir else None
            signature = [hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),path.stat().st_size]
            saved = json.loads(checkpoint.read_text()) if checkpoint and checkpoint.is_file() else None
            if saved and saved['signature'] == signature:
                sub, count = saved['rows'],saved['count']
            else:
                sub, count = study.scan(path,day,peak)
                if checkpoint:
                    checkpoint.parent.mkdir(parents=True,exist_ok=True)
                    checkpoint.write_text(json.dumps({'signature':signature,'rows':sub,'count':count}))
            rows.extend(sub)
            counts[day+' '+peak] = count
            stat = evaluate(sub)
            print(day,peak,count,'borderline',stat['borderline'],'30s',stat['thresholds']['fallback_5min']['30'],flush=True)
    rows.sort(key=lambda r:(r['day'],r['ready'],r['bus']))
    test = [r for r in rows if r['day'] != DAYS[0]]
    report = {
        'schema':1,'training_dates':study.model.metadata['train'],'development_date':DAYS[0], 'test_dates':DAYS[1:],
        'thresholds_seconds':GRACES, 'nominated_threshold_seconds':30, 'routes':study.routes,
        'coverage':counts, 'development':evaluate([r for r in rows if r['day']==DAYS[0]]),
        'held_out':evaluate(test), 'unique_test_vehicles':len({r['bus'] for r in test}),
        'test_by_peak':{p:evaluate([r for r in test if r['peak']==p]) for p in PEAKS},
        'test_by_day':{d:evaluate([r for r in test if r['day']==d]) for d in DAYS[1:]},
        'test_by_route':{r:evaluate([v for v in test if v['route']==r])['thresholds']['fallback_5min']['30']
                         for r in sorted({v['route'] for v in test})},
        'test_gap_bands':bands(test),
        'test_gap_bands_by_peak':{p:bands([r for r in test if r['peak']==p]) for p in PEAKS},
        'gtfs_sha256':hashlib.sha256(study.gtfs.read_bytes()).hexdigest(),
        'fitted_model_sha256':hashlib.sha256((study.data/'eta_model.json.gz').read_bytes()).hexdigest(),
    }
    args.output.write_text(json.dumps(report,indent=2),encoding='utf-8')
    with gzip.GzipFile(filename=str(args.rows),mode='wb',mtime=0) as out:
        out.write(json.dumps(rows,separators=(',',':')).encode())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory',type=Path,help='Contains the three extracted daily SQLite files')
    parser.add_argument('--cache-dir',type=Path,help='Optional scratch folder for restartable raw scans')
    parser.add_argument('--output',type=Path,default=ROOT/'data'/'near-miss-peak.json')
    parser.add_argument('--rows',type=Path,default=ROOT/'data'/'near-miss-opportunities.json.gz')
    run(parser.parse_args())
