"""Extract directed, GPS-timed stop-to-stop rides from the official October archive.

Run this offline, with --scratch on a volume with at least 8 GiB free. A single
archive member is opened at a time. No raw coordinates or vehicle IDs are
written to the dated labels. The live and previously evaluated models are read-only.
"""
import argparse
import csv
from datetime import date, datetime, time, timedelta
import gzip
import hashlib
import io
import inspect
import json
from pathlib import Path
import sys
from time import perf_counter
from zipfile import ZipFile

import numpy as np
from numba import njit, types
from numba.typed import Dict

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.fleet_gps import FIX, reader
from tools.month_boarding_scan import ARCHIVE, MONTH, PREVIOUS_EXTRACTS, archive_members, daily_database
from tools.october_census import read_census
from tools.train_eta import FRESHNESS, RADIUS_M, boundary, gtfs_routes, open_db
from transit.eta import IST


COLUMNS = ('route', 'from_stop', 'to_stop', 'start', 'end', 'issue_received', 'received', 'seconds', 'scheduled')
VERSION = 'delhi-directed-traversals-v1'
CELL = .0007  # One grid cell is wider than 60 m in both axes across Delhi.
GRID_SIDE = 4096


def midnight(day):
    return int(datetime.combine(date.fromisoformat(day), time(), IST).timestamp())


def index_stops(gtfs, selected=None):
    if selected is None:
        with ZipFile(gtfs) as archive:
            with archive.open('routes.txt') as routes:
                selected = {row['route_id'] for row in csv.DictReader(io.TextIOWrapper(routes, encoding='utf-8-sig'))}
    routes = gtfs_routes(gtfs, selected)
    centres, scale, numbers, eligible, durations = [], np.zeros((65536, 2)), [], [], []
    offsets, trees, keys, items = {}, {}, [], []
    for name, (stops, schedules, counts, tree, distance_scale) in routes.items():
        route = int(name)
        start = len(numbers)
        offsets[route], trees[route] = start, tree
        centres.extend(tree.data)
        scale[route] = distance_scale
        numbers.extend(int(s) for s in stops)
        eligible.extend(counts[s] == 1 for s in stops)
        durations.extend([*schedules, 0])
        cells = np.floor((tree.data / distance_scale - [28, 76]) / CELL).astype('i4')
        stops_idx = np.arange(start, start + len(stops), dtype='i4')
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                mapped = (np.int64(route) * GRID_SIDE + cells[:, 0] + dy) * GRID_SIDE + cells[:, 1] + dx
                keys.append(mapped); items.append(stops_idx)
    keys, items = np.concatenate(keys), np.concatenate(items)
    order = np.argsort(keys, kind='stable')
    unique, starts = np.unique(keys[order], return_index=True)
    starts = np.append(starts, len(keys)).astype('i4')
    grid = (unique, starts, items[order], np.asarray(centres), scale)
    stops = (np.asarray(numbers, dtype='i4'), np.asarray(eligible), np.asarray(durations, dtype='i4'))
    return grid, stops, (offsets, trees)


@njit(nogil=True)
def nearest(fixes, keys, starts, members, positions, scale):
    result = np.full(len(fixes), -1, dtype=np.int32)
    for i, point in enumerate(fixes):
        r = point.route
        if scale[r, 0] == 0:
            continue
        ycell = int(np.floor((point.lat - 28) / CELL))
        xcell = int(np.floor((point.lon - 76) / CELL))
        key = (r * GRID_SIDE + ycell) * GRID_SIDE + xcell
        index = np.searchsorted(keys, key)
        if index >= len(keys) or keys[index] != key:
            continue
        y, x = point.lat * scale[r, 0], point.lon * scale[r, 1]
        best = RADIUS_M ** 2
        winner = -1
        for slot in range(starts[index], starts[index + 1]):
            k = members[slot]
            dist = (y - positions[k, 0]) ** 2 + (x - positions[k, 1]) ** 2
            if dist < best or (dist == best and winner == -1):
                best, winner = dist, k
            elif dist == best:
                winner = -2  # Exact tie uses the reference cKDTree's selection.
        result[i] = winner
    return result


@njit(nogil=True)
def passages(fixes, indices, stops, unique, schedules, states):
    label = np.empty((len(fixes), 9), np.int32)
    n = 0
    for i, point in enumerate(fixes):
        route, obs, recv, idx = point.route, np.int64(point.obs), np.int64(point.recv), np.int64(indices[i])
        key = (np.uint64(route), point.bus, point.trip)
        previous, previous_idx, hit, hit_obs, hit_recv = states.get(key, (-1, -1, -1, -1, -1))
        if obs == previous:
            continue
        if obs < previous:
            raise ValueError('An observation arrived behind the reorder watermark')
        if previous == -1 or obs - previous > FRESHNESS:
            hit, hit_obs, hit_recv = -1, -1, -1
        elif idx >= 0 and idx != previous_idx:
            if not unique[idx]:
                hit, hit_obs, hit_recv = -1, -1, -1
            elif idx != hit:
                if hit >= 0 and idx == hit + 1:
                    duration, sched = obs - hit_obs, schedules[hit]
                    if 5 <= duration <= 1200 and 5 <= sched <= 1800:
                        label[n, 0], label[n, 1], label[n, 2] = route, stops[hit], stops[idx]
                        label[n, 3], label[n, 4], label[n, 5] = hit_obs, obs, hit_recv
                        label[n, 6], label[n, 7], label[n, 8] = max(hit_recv, recv), duration, sched
                        n += 1
                hit, hit_obs, hit_recv = idx, obs, recv
        states[key] = (obs, idx, hit, hit_obs, hit_recv)
    return label[:n]


@njit(nogil=True)
def prune(states, watermark):
    for key in list(states):
        if states[key][0] < watermark - FRESHNESS:
            del states[key]


class Matcher:
    def __init__(self, model):
        self.grid, self.stops, self.reference = model
        self.pending = np.empty(0, dtype=FIX)
        self.states = Dict.empty(types.UniTuple(types.uint64, 3), types.UniTuple(types.int64, 5))

    def advance(self, fixes, watermark):
        stream = np.concatenate((self.pending, fixes))
        ready = stream['obs'] < watermark
        self.pending = stream[~ready]
        work = stream[ready]
        if not len(work):
            return np.empty((0, len(COLUMNS)), np.int32)
        work = work[np.argsort(work['obs'], kind='stable')]
        located = nearest(work, *self.grid)
        tied = np.nonzero(located == -2)[0]
        if len(tied):
            offsets, trees = self.reference
            for route in np.unique(work['route'][tied]):
                rows = tied[work['route'][tied] == route]
                coords = np.column_stack((work['lat'][rows], work['lon'][rows])) * self.grid[-1][route]
                distances, choices = trees[route].query(coords)
                located[rows] = np.where(distances <= RADIUS_M, offsets[route] + choices, -1)
        return passages(work, located, *self.stops, self.states)


def dump_json(path, data):
    part = path.with_suffix('.json.building')
    part.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
    part.replace(path)


def dump_labels(path, matrix):
    part = path.with_suffix('.npz.building')
    with part.open('wb') as out:
        np.savez_compressed(out, **{k: matrix[:, i] for i, k in enumerate(COLUMNS)})
    part.replace(path)


def file_digest(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def validate_checkpoint(labels, day, member):
    meta = json.loads((labels / (day + '.json')).read_bytes())
    target = labels / (day + '.npz')
    if (meta['method'] != VERSION or meta['day'] != day or meta['source_member_crc32'] != member['crc32']
            or set(meta['hourly_raw']) != {f'{i:02}' for i in range(24)} or sum(meta['hourly_raw'].values()) != meta['raw']):
        raise ValueError('Incompatible source or clock coverage on ' + day)
    sha = file_digest(target)
    if 'labels_sha256' in meta and meta['labels_sha256'] != sha:
        raise ValueError('Changed label archive for ' + day)
    with np.load(target, allow_pickle=False) as columns:
        if tuple(columns.files) != COLUMNS:
            raise ValueError('Incompatible label schema for ' + day)
        # Reading each member checks its ZIP CRC, including older checkpoints with no external SHA.
        for name in COLUMNS:
            values = columns[name]
            if values.dtype != np.int32 or values.shape != (meta['labels'],):
                raise ValueError('Invalid label column for ' + day + ': ' + name)
    return {**meta, 'labels_sha256': sha}


def pin_protocol(labels, gtfs):
    core = (index_stops, nearest.py_func, passages.py_func, prune.py_func, Matcher, DatedLabels.add, scan_range)
    fingerprint = hashlib.sha256('\n'.join(inspect.getsource(function) for function in core).encode()).hexdigest()
    current = {'method': VERSION, 'official_gtfs_sha256': file_digest(gtfs), 'matching_code_sha256': fingerprint,
               'numeric_sqlite_reader_sha256': file_digest(ROOT/'tools/fleet_gps.py'),
               'reference_trainer_sha256': file_digest(ROOT/'tools/train_eta.py'), 'columns': list(COLUMNS)}
    target = labels / 'protocol.json'
    if target.exists() and current != json.loads(target.read_bytes()):
        raise ValueError('Scratch directory holds labels from another timetable or matching method.')
    dump_json(target, current)
    return current


class DatedLabels:
    def __init__(self, directory, dates):
        self.directory, self.dates, self.started = directory, set(dates), set()

    def add(self, matrix, day):
        if not len(matrix):
            return
        previous = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
        earlier = matrix[:, 4] < midnight(day)
        for key, mask in ((previous, earlier), (day, ~earlier)):
            if key in self.dates and mask.any():
                rows = matrix[mask]
                assert rows[:, 4].min() >= midnight(key) and rows[:, 4].max() < midnight(key) + 86400
                with (self.directory / (key + '.i32')).open('ab' if key in self.started else 'wb') as out:
                    rows.tofile(out)
                self.started.add(key)

    def finalize(self, day, meta):
        path = self.directory / (day + '.i32')
        rows = np.memmap(path, dtype='i4', mode='r').reshape(-1, len(COLUMNS)) if day in self.started else np.empty((0, 9), np.int32)
        dump_labels(self.directory / (day + '.npz'), rows)
        meta.update(labels=int(len(rows)), routes_with_labels=int(len(np.unique(rows[:, 0]))),
                    labels_sha256=file_digest(self.directory / (day + '.npz')))
        dump_json(self.directory / (day + '.json'), meta)
        del rows
        if day in self.started:
            path.unlink()
        print('Completed', day, meta['raw'], 'raw messages;', meta['labels'], 'directed rides;', meta['scan_seconds'], 'scan seconds', flush=True)


def scan_range(path, day, matcher, emit, start, end, batch, hour_done=None):
    native = reader()
    raw_total, fresh = np.zeros(65536, dtype='int64'), 0
    by_hour = {}
    day_epoch = midnight(day)
    with open_db(path) as db:
        for hour in range(start // 3600, (end - 1) // 3600 + 1):
            lo_seconds, hi_seconds = max(start, hour * 3600), min(end, (hour + 1) * 3600)
            def cut(seconds):
                if seconds == 86400:
                    return db.execute('SELECT max(rowid)+1 FROM vehicle_feed').fetchone()[0] or 1
                return boundary(db, f'{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}')
            lo, hi = cut(lo_seconds), cut(hi_seconds)
            by_hour[f'{hour:02}'] = hi - lo
            for first in range(lo, hi, batch):
                last = min(first + batch, hi)
                raw, valid, fixes = native(path, first, last, day_epoch, hour, last - first)
                if int(raw.sum()) != last - first:
                    raise ValueError('Source row count differs from the requested SQLite range')
                raw_total += raw
                fresh += len(fixes)
                if len(fixes):
                    emit(matcher.advance(fixes, int(fixes[-1]['recv']) - FRESHNESS), day)
            watermark = day_epoch + hi_seconds - FRESHNESS
            emit(matcher.advance(np.empty(0, dtype=FIX), watermark), day)
            prune(matcher.states, watermark)
            if hour_done:
                hour_done(hour)
    return raw_total, by_hour, fresh


def process(args):
    from tools.fleet_gps import MISSING_ROUTE
    labels = args.scratch / 'labels'
    labels.mkdir(parents=True, exist_ok=True)
    if args.batch_rows < 1:
        raise ValueError('--batch-rows must be positive')
    if args.fixture_routes and not args.fixture_day:
        raise ValueError('A route whitelist is allowed only for a parity fixture')
    if not args.fixture_day:
        protocol = pin_protocol(labels, args.gtfs)
        if args.pin_protocol_only:
            return
        if args.verify_checkpoints:
            members = archive_members(args.seven, args.archive)
            dates = {day: validate_checkpoint(labels, day, members[day]) for day in MONTH}
            dump_json(args.scratch / 'complete-label-manifest.json', {'protocol': protocol, 'dates': dates})
            print('Verified', len(dates), 'dates and', sum(d['labels'] for d in dates.values()), 'directed rides.')
            return
    selected = None
    if args.fixture_routes:
        with gzip.open(args.fixture_routes, 'rt') as f:
            selected = list(json.load(f)['routes'])
    t0 = perf_counter()
    model = index_stops(args.gtfs, selected)
    matcher = Matcher(model)
    members = archive_members(args.seven, args.archive)
    print('Loaded', len(model[2][0]), 'directed GTFS patterns in', round(perf_counter() - t0, 1), 'seconds', flush=True)
    if args.fixture_day:
        def sec(clock):
            h, m, s = map(int, clock.split(':'))
            return h*3600 + m*60 + s
        chunks = []
        with daily_database(args.seven, args.archive, members[args.fixture_day], args.existing, args.scratch) as (path, _):
            t0 = perf_counter()
            counts, _, _ = scan_range(path, args.fixture_day, matcher, lambda a, _: chunks.append(a), sec(args.fixture_start), sec(args.fixture_end), args.batch_rows)
            chunks.append(matcher.advance(np.empty(0, dtype=FIX), 2**31 - 1))
        rows = np.concatenate(chunks)
        dump_labels(args.scratch / 'fixture.npz', rows)
        print('Fixture:', int(counts.sum()), 'broadcasts;', len(rows), 'labels;', round(perf_counter()-t0, 2), 'seconds including compilation', flush=True)
        return
    pending = []
    for day in MONTH:
        fingerprint = labels / (day + '.json')
        if fingerprint.exists():
            validate_checkpoint(labels, day, members[day])
            if pending:
                raise ValueError('Expected dated checkpoints in chronological order')
        else:
            pending.append(day)
    if not pending:
        return
    writer = DatedLabels(labels, pending)
    first = MONTH.index(pending[0])
    # A stationary or circling run can repeatedly re-enter its last stop without
    # acquiring a new passage. A fixed lookback could count a false new passage.
    # Restore the exact transient state; completed outputs stay untouched.
    for previous in MONTH[:first]:
        print('Restoring observation state through', previous, flush=True)
        with daily_database(args.seven, args.archive, members[previous], args.existing, args.scratch) as (path, _):
            scan_range(path, previous, matcher, lambda matrix, day: None, 0, 86400, args.batch_rows)
    last_day, last_meta = None, None
    for day in pending:
        t0 = perf_counter()
        with daily_database(args.seven, args.archive, members[day], args.existing, args.scratch) as (path, source):
            print('Scanning', day, source, flush=True)
            def report_hour(hour):
                if hour == 0 and last_day is not None:
                    writer.finalize(last_day, last_meta)
                if hour in (5, 11, 17, 23):
                    print(day, f'{hour+1:02}:00', round(perf_counter()-t0, 1), 'seconds', flush=True)
            total, by_hour, fresh = scan_range(path, day, matcher, writer.add, 0, 86400, args.batch_rows, report_hour)
        source_counts = read_census(day)
        if not source_counts['complete'] or source_counts['archive_member']['crc32'] != members[day]['crc32']:
            raise ValueError('Independent census belongs to another source on ' + day)
        census = source_counts['census']
        by_route = {str(r) if r != MISSING_ROUTE else 'missing': int(total[r]) for r in np.flatnonzero(total)}
        if (int(total.sum()) != census['raw_records'] or {h:n for h,n in by_hour.items() if n} != census['by_hour']
                or by_route != census['by_route']):
            raise ValueError('Source rows differ from the independent census for ' + day)
        last_day, last_meta = day, dict(method=VERSION, day=day, raw=int(total.sum()), fresh_identified_positions=fresh,
                                        hourly_raw=by_hour, patterns=len(model[2][0]), source_member_crc32=members[day]['crc32'],
                                        scan_seconds=round(perf_counter()-t0, 2))
    writer.add(matcher.advance(np.empty(0, dtype=FIX), 2**31-1), last_day)
    writer.finalize(last_day, last_meta)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scratch', type=Path, required=True)
    parser.add_argument('--gtfs', type=Path, default=ROOT/'delhi_buses_static_gtfs_v1.zip')
    parser.add_argument('--archive', type=Path, default=ARCHIVE)
    parser.add_argument('--existing', type=Path, default=PREVIOUS_EXTRACTS)
    parser.add_argument('--seven', type=Path, default=Path('C:/Program Files/7-Zip/7z.exe'))
    parser.add_argument('--batch-rows', type=int, default=125000)
    parser.add_argument('--pin-protocol-only', action='store_true', help='Record or check the timetable and matching-code hashes without scanning.')
    parser.add_argument('--verify-checkpoints', action='store_true', help='Check all 31 compressed label files against their receipts and write a private manifest.')
    parser.add_argument('--fixture-day', choices=MONTH)
    parser.add_argument('--fixture-routes', type=Path)
    parser.add_argument('--fixture-start', default='08:30:00')
    parser.add_argument('--fixture-end', default='08:35:00')
    process(parser.parse_args())
