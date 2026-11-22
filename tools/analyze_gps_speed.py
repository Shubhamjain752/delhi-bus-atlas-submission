"""Reproduce the directed bus-speed audit and independently dated ETA comparison."""

import argparse
from bisect import bisect_left
from collections import Counter, defaultdict
import csv
from datetime import date, datetime, time
import hashlib
import io
import json
from pathlib import Path
from statistics import median
import sys
import zipfile

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.train_eta import boundary, clock, extract, format_clock, gtfs_routes, metrics, open_db
from transit.eta import EtaModel, IST, read_data


SCALE = np.array([111195., 111195. * np.cos(np.deg2rad(28.60))])
ORIGIN = np.array([28.6, 77.2])
MODELS = ('timetable', 'historical', 'current', 'candidate')


def iso(at):
    return datetime.fromtimestamp(int(at),IST).isoformat()


def project_stop(line, point):
    """Distance along the route in metres, and perpendicular offset to the stop."""
    vectors = np.diff(line,axis=0)
    lengths = np.linalg.norm(vectors,axis=1)
    if not len(lengths):
        return None
    t = np.divide(np.einsum('ij,ij->i',point-line[:-1],vectors),lengths**2,
                  out=np.zeros_like(lengths),where=lengths>0).clip(0,1)
    distances = np.linalg.norm(line[:-1]+vectors*t[:,None]-point,axis=1)
    closest = int(distances.argmin())
    offset = float(distances[closest])
    if offset > 60:
        return None
    chain = np.r_[0.,np.cumsum(lengths[:-1])] + lengths*t
    # Parallel passes on loops can be nearly equidistant yet kilometres apart.
    if np.any((distances <= offset + 8) & (np.abs(chain-chain[closest]) > 150)):
        return None
    return float(chain[closest]),offset


def recent_before(events, at, span):
    """Use both clocks. A same-second or later receipt cannot be a predictor."""
    lo = bisect_left(events,(at-span,))
    hi = bisect_left(events,(at,))
    return [v for v in events[lo:hi] if at-span <= v[1] < at]


def neighbor_prediction(history, current, same_count, ratios, link_count):
    if len(ratios) < 3 or link_count < 2:
        return current
    multiplier = min(2.,max(.5,median(ratios)))
    # Keep target-link evidence intact; adjust only the surviving historical share.
    weight = (5 / (5 + same_count)) * (len(ratios) / (len(ratios) + 8))
    return max(1,round(current + history * weight * (multiplier-1)))


def feed_links(path, selected):
    with zipfile.ZipFile(path) as feed:
        def rows(name):
            return csv.DictReader(io.TextIOWrapper(feed.open(name),encoding='utf-8-sig'))
        stops = {r['stop_id']:(r['stop_name'],[float(r['stop_lat']),float(r['stop_lon'])]) for r in rows('stops.txt')}
        names = {r['route_id']:r['route_long_name'] for r in rows('routes.txt')}
        chosen = {}
        for r in rows('trips.txt'):
            rid, trip = r['route_id'], r['trip_id']
            if rid in selected and (rid not in chosen or ('_FULL_' not in chosen[rid][0] and '_FULL_' in trip)):
                chosen[rid] = (trip,r['shape_id'])
        wanted = {trip:rid for rid,(trip,_) in chosen.items()}
        patterns = defaultdict(list)
        for r in rows('stop_times.txt'):
            if r['trip_id'] in wanted:
                patterns[wanted[r['trip_id']]].append((int(r['stop_sequence']),r['stop_id'],clock(r['arrival_time'])))
        shapes = defaultdict(list)
        wanted_shapes = {shape for _,shape in chosen.values()}
        for r in rows('shapes.txt'):
            if r['shape_id'] in wanted_shapes:
                shapes[r['shape_id']].append((int(r['shape_pt_sequence']),float(r['shape_pt_lat']),float(r['shape_pt_lon'])))
    links, neighbors = {}, {}
    for rid, pattern in patterns.items():
        ordered = sorted(pattern)
        ids = [s for _,s,_ in ordered]
        counts = Counter(ids)
        shape_id = chosen[rid][1]
        shape = sorted(shapes.get(shape_id,[]))
        if len(shape) < 2:
            continue
        xy = (np.asarray([s[1:] for s in shape])-ORIGIN)*SCALE
        chainage = np.r_[0.,np.cumsum(np.linalg.norm(np.diff(xy,axis=0),axis=1))]
        positions = {s:project_stop(xy,(np.array(stops[s][1])-ORIGIN)*SCALE) for s in set(ids) if counts[s]==1}
        directed = [(rid,a,b) for a,b in zip(ids,ids[1:])]
        for i,key in enumerate(directed):
            _,a,b = key
            p,q = positions.get(a),positions.get(b)
            if not p or not q:
                continue
            length = q[0]-p[0]
            if not 100 <= length <= 5000:
                continue
            chord = np.linalg.norm((np.array(stops[a][1])-stops[b][1])*SCALE)
            if length > 4 * max(chord,30):
                continue
            fractions = np.linspace(p[0],q[0],min(25,max(3,int(length/40)+1)))
            points = np.column_stack([np.interp(fractions,chainage,xy[:,n]) for n in (0,1)]) / SCALE + ORIGIN
            links[key] = {'route_name':names[rid],'from_stop':stops[a][0],'to_stop':stops[b][0],
                          'shape_id':shape_id,'shape_distance_m':round(length,1),
                          'endpoint_offsets_m':[round(p[1],1),round(q[1],1)],
                          'scheduled_seconds':ordered[i+1][2]-ordered[i][2],
                          'geometry':points.round(6).tolist()}
            neighbors[key] = directed[max(0,i-2):i] + directed[i+1:i+3]
    return links,neighbors


def kph(distance, seconds):
    return round(3.6*distance/seconds,2)


def benchmark(label, day, fitted, replay, links, neighbors):
    midnight = int(datetime.combine(date.fromisoformat(day),time(),IST).timestamp())
    model = EtaModel(fitted,replay)
    if date.fromisoformat(day) <= model.last_train_day:
        raise ValueError('Holdout day must be after the last training date')
    observations = replay['segments']
    qualified, by_link = [], defaultdict(list)
    for receipt, arrival, route, a, b, duration in observations:
        key = (route,a,b)
        if key not in links:
            continue
        start = arrival-duration
        if not midnight <= start < midnight+86400 or receipt < arrival:
            continue
        sched = links[key]['scheduled_seconds']
        hist = model.duration(*key,start-midnight,sched)[0]
        by_link[key].append((receipt,arrival,duration,hist))
        qualified.append((start,arrival,receipt,key,duration,hist))
    for events in by_link.values():
        events.sort()
    predictions, evidence_indices, examples = [], [], {}
    speeds, recent_speeds, lengths, route_ids = [], [], [], []
    total_slow = 0
    for start,arrival,receipt,key,actual,hist in sorted(qualified):
        desc = links[key]
        distance = desc['shape_distance_m']
        sched = desc['scheduled_seconds']
        current = model.duration(*key,start-midnight,sched,start)[0]
        same = recent_before(by_link[key],start,1800)
        adjacent = [recent_before(by_link[n],start,1200) for n in neighbors[key] if n in by_link]
        adjacent = [v for v in adjacent if v]
        ratios = [val[2]/val[3] for group in adjacent for val in group]
        candidate = neighbor_prediction(hist,current,len(same),ratios,len(adjacent))
        if len(ratios)>=3 and len(adjacent)>=2:
            evidence_indices.append(len(predictions))
        predictions.append((actual,sched,hist,current,candidate))
        speeds.append(kph(distance,actual))
        lengths.append(distance)
        route_ids.append(key[0])
        if len(same)<3:
            continue
        recent_duration = median([o[2] for o in same])
        ratio = recent_duration / hist
        recent_speeds.append(kph(distance,recent_duration))
        total_slow += ratio >= 1.4
        # Choose examples using signals available when the traveler asked.
        # Never select on realised accuracy, error direction or the target duration.
        kind = 'slow' if ratio >= 1.4 else ('usual' if .95 <= ratio <= 1.05 else None)
        if kind and kind not in examples and 250 <= distance <= 2000 and len(adjacent)>=2 and len(ratios)>=3:
            near = [v for group in adjacent for v in group]
            examples[kind] = {
                'profile':label,'condition':kind,'route_id':key[0],'from_stop_id':key[1],'to_stop_id':key[2],**desc,
                'forecast_at_ist':iso(start),'observed_arrival_ist':iso(arrival),'outcome_received_ist':iso(receipt),
                'observed_seconds':actual,'historical_seconds':hist,'current_model_seconds':current,
                'candidate_seconds':candidate,'recent_median_seconds':recent_duration,
                'historical_speed_kmh':kph(distance,hist),'recent_speed_kmh':kph(distance,recent_duration),
                'observed_speed_kmh':kph(distance,actual),'slowdown_ratio':round(ratio,3),
                'same_segment_prior_traversals':len(same),
                'same_segment_first_receipt_ist':iso(same[0][0]),'same_segment_last_receipt_ist':iso(same[-1][0]),
                'last_neighbor_receipt_ist':iso(max(v[0] for v in near)),
                'neighbor_traversals':len(ratios),'distinct_neighbor_segments':len(adjacent)}
    values = np.asarray(predictions)
    def score(v):
        return {name:metrics(v[:,0],v[:,i+1]) for i,name in enumerate(MODELS)}
    def quantiles(s):
        return {k:round(float(np.percentile(s,p)),2) for k,p in [('p10',10),('median',50),('p90',90)]} if s else {}
    implied_speeds = 3.6*np.asarray(lengths)[:,None]/values
    speed_errors = {}
    for i,name in enumerate(MODELS):
        err = implied_speeds[:,i+1]-implied_speeds[:,0]
        speed_errors[name] = {'mae_kmh':round(float(np.abs(err).mean()),2),
                              'p90_absolute_error_kmh':round(float(np.percentile(np.abs(err),90)),2),
                              'signed_bias_kmh':round(float(err.mean()),2)}
    # Paired, route-clustered resampling retains within-route correlations. It
    # describes route mix on this day, not uncertainty over unobserved dates.
    delta = np.abs(values[:,4]-values[:,0])-np.abs(values[:,3]-values[:,0])
    ids,idx = np.unique(route_ids,return_inverse=True)
    sums,counts = np.bincount(idx,weights=delta),np.bincount(idx)
    samples = np.random.default_rng(221026).integers(0,len(ids),size=(2000,len(ids)))
    bootstrap = sums[samples].sum(axis=1)/counts[samples].sum(axis=1)
    paired = {'candidate_minus_current_mae_s':round(float(delta.mean()),3),
              'routes_with_lower_candidate_mae':int((sums<0).sum()),
              'routes_with_higher_candidate_mae':int((sums>0).sum()),
              'route_cluster_bootstrap_95pct_s':np.percentile(bootstrap,[2.5,97.5]).round(3).tolist()}
    return {'holdout':day,'train':fitted['metadata']['train'],'windows_ist':fitted['metadata']['windows_ist'],
            'coverage':{'total_holdout_segments':len(observations),'distance_qualified_segments':len(values),
                        'distinct_directed_route_stop_pairs':len(by_link),'routes':len({k[0] for k in by_link}),
                        'neighbor_evidence_segments':len(evidence_indices),'at_least_three_same_link':len(recent_speeds),
                        'slow_queries_at_1_4x':total_slow,'observed_speed_over_80_kmh':sum(s>80 for s in speeds)},
            'metrics':score(values),'with_neighbor_evidence':score(values[evidence_indices]),
            'speed_metrics':speed_errors,'paired_comparison':paired,
            'observed_speed_kmh':quantiles(speeds),'recent_speed_kmh':quantiles(recent_speeds)},list(examples.values())


def raw_speed_audit(path):
    """Audit exactly three five-minute windows, independent of route and ticket data."""
    day = '2026-10-22'
    midnight = int(datetime.combine(date.fromisoformat(day),time(),IST).timestamp())
    raw_frames = []
    with open_db(path) as db:
        for h in (8,12,17):
            t = h*3600+30*60
            a,b = boundary(db,format_clock(t)),boundary(db,format_clock(t+300))
            raw_frames.append(pd.read_sql_query(
                'SELECT vehicle_id,trip_id,route_id,speed,CAST(lat AS REAL) lat,CAST(lng AS REAL) lon,'
                'CAST(timestamp AS INTEGER) observed,time FROM vehicle_feed WHERE rowid>=? AND rowid<? '
                'AND time>=? AND time<?',db,params=(a,b,format_clock(t),format_clock(t+300))))
    frame = pd.concat(raw_frames,ignore_index=True)
    raw_count = len(frame)
    frame['receipt'] = frame.time.map({t:midnight+clock(t) for t in frame.time.unique()})
    frame = frame.dropna(subset=['route_id','trip_id','vehicle_id','speed'])
    for field in ('vehicle_id','trip_id'):
        frame = frame[~frame[field].astype(str).str.strip().str.lower().isin(['','null','none'])]
    frame = frame[frame.lat.between(28,30) & frame.lon.between(76,78) & frame.speed.ge(0) &
                  (frame.receipt-frame.observed).between(0,90)]
    before = len(frame)
    keys = ['vehicle_id','trip_id','route_id']
    frame = frame.sort_values(keys+['observed','receipt']).drop_duplicates(keys+['observed'])
    previous = frame.groupby(keys,sort=False)[['observed','lat','lon','speed']].shift()
    dt = frame.observed-previous.observed
    metres = np.hypot((frame.lat-previous.lat)*SCALE[0],(frame.lon-previous.lon)*SCALE[1])
    inferred = 3.6 * metres / dt
    usable = dt.between(10,60) & metres.ge(30) & inferred.le(120)
    measured = (frame.speed[usable]+previous.speed[usable])/2
    dif = inferred[usable]
    index = frame[usable].sort_values(['observed','route_id']).index[0]
    p,r = previous.loc[index],frame.loc[index]
    return {'date':day,'windows_ist':[['08:30','08:35'],['12:30','12:35'],['17:30','17:35']],
            'raw_rows':raw_count,'fresh_valid_before_dedup':before,'unique_fresh_pings':len(frame),
            'zero_speed_pings':int(frame.speed.eq(0).sum()),'num_usable_moving_intervals':len(measured),
            'raw_speed_median':round(float(frame.speed.median()),2),'raw_speed_p95':round(float(frame.speed.quantile(.95)),2),
            'median_chord_speed_kmh':round(float(dif.median()),2),
            'median_absolute_difference_if_raw_mps_kmh':round(float((3.6*measured-dif).abs().median()),2),
            'median_absolute_difference_if_raw_kmh_kmh':round(float((measured-dif).abs().median()),2),
            'first_qualifying_interval':{'route_id':str(int(r.route_id)),
              'bus_run_alias':hashlib.blake2s(f'{r.vehicle_id}|{r.trip_id}'.encode(),digest_size=8).hexdigest(),
              'from_ist':iso(p.observed),'to_ist':iso(r.observed),
              'from':[round(p.lat,6),round(p.lon,6)],'to':[round(r.lat,6),round(r.lon,6)],
              'seconds':int(dt.loc[index]),'chord_m':round(metres.loc[index],1),
              'derived_kmh':round(dif.loc[index],2),'reported_raw_endpoints':[round(p.speed,3),round(r.speed,3)],
              'mean_reported_if_mps_kmh':round(3.6*measured.loc[index],2)}}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data-dir',type=Path,default=Path('data'))
    p.add_argument('--gtfs',type=Path,default=Path('delhi_buses_static_gtfs_v1.zip'))
    p.add_argument('--confirmation-db',type=Path,required=True,help='bus_movements_2026_10_22.db')
    p.add_argument('--confirmation-replay',type=Path,help='Optional trusted cache produced from the same October 22 DB')
    args=p.parse_args()
    if args.confirmation_db.name != 'bus_movements_2026_10_22.db':
        p.error('Expected the separately held-out 22 October database')
    profiles = {}
    for name,folder in [('weekday',args.data_dir),('weekend',args.data_dir/'weekend')]:
        fit = read_data(folder/'eta_model.json.gz')
        profiles[name] = (fit['metadata']['holdout'],fit,read_data(folder/'eta_replay.json.gz'))
    fit = profiles['weekday'][1]
    if args.confirmation_replay:
        replay = read_data(args.confirmation_replay)
        if replay['date'] != '2026-10-22':
            raise ValueError('Incorrect cached holdout date')
    else:
        routes = gtfs_routes(args.gtfs,fit['routes'])
        events,_,count=extract(args.confirmation_db,'2026-10-22',routes)
        replay={'segments':[v[:6] for v in events],'passages':[],'raw_pings_selected':count}
    profiles['confirmation']=('2026-10-22',fit,replay)
    selected={r for _,f,_ in profiles.values() for r in f['routes']}
    links,neighbors=feed_links(args.gtfs,selected)
    report={'schema_version':1,'units':{'distance':'metres','time':'seconds','speed':'km/h','clocks':'IST'},
            'distance_method':'Projected distance along ordered GTFS v32 shape between stop projections; offsets <=60 m, unique chainage, segments 100-5000 m.',
            'units_reference':'https://gtfs.org/documentation/realtime/reference/#message-position',
            'gtfs_sha256':hashlib.sha256(args.gtfs.read_bytes()).hexdigest(),
            'windows_ist':{name:fit['metadata']['windows_ist'] for name,(_,fit,_) in profiles.items()},
            'raw_gps':raw_speed_audit(args.confirmation_db),'profiles':{},'examples':[]}
    for label,(day,fit,replay) in profiles.items():
        result,examples=benchmark(label,day,fit,replay,links,neighbors)
        if 'raw_pings_selected' in replay:
            result['coverage']['raw_pings_selected']=replay['raw_pings_selected']
        report['profiles'][label]=result
        report['examples'].extend(examples)
        print(label,result['coverage'],result['metrics'],flush=True)
    output=args.data_dir/'gps-speed.json'
    temp=output.with_suffix('.tmp')
    temp.write_text(json.dumps(report,indent=2,allow_nan=False),encoding='utf-8')
    temp.replace(output)


if __name__ == '__main__':
    main()
