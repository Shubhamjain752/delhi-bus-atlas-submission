"""Fit and evaluate Delhi GPS stop-passage models from extracted daily SQLite files."""

import argparse
from collections import Counter, defaultdict
from contextlib import closing
import csv
from datetime import date, datetime, time
import gzip
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import zipfile

import numpy as np
import pandas as pd
from scipy.spatial import cKDTree

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from transit.eta import BIN_SECONDS, EtaModel, IST, read_data


DEFAULT_WINDOWS = [(8 * 3600, 9 * 3600 + 1800), (12 * 3600, 13 * 3600 + 1800),
                   (17 * 3600, 18 * 3600 + 1800)]
RADIUS_M = 60
FRESHNESS = 90


def clock(value):
    h, m, s = map(int, value.split(":"))
    return h * 3600 + m * 60 + s


def format_clock(seconds):
    return f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}"


def store(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path.with_suffix(".tmp"), "wt", encoding="utf-8", compresslevel=5) as out:
        json.dump(obj, out, separators=(",", ":"), allow_nan=False)
    path.with_suffix(".tmp").replace(path)


def open_db(path):
    return closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True))


def boundary(db, goal):
    """Daily databases append polling batches in IST receipt-clock order."""
    lo, hi = 1, db.execute("SELECT max(rowid) FROM vehicle_feed").fetchone()[0] + 1
    while lo < hi:
        mid = (lo + hi) // 2
        value = db.execute("SELECT time FROM vehicle_feed WHERE rowid=?", (mid,)).fetchone()
        if value and value[0] < goal:
            lo = mid + 1
        else:
            hi = mid
    return lo


def candidate_routes(path, limit, windows=DEFAULT_WINDOWS):
    counts = Counter()
    with open_db(path) as db:
        for start, _ in windows:
            row = boundary(db, format_clock(start + 900))
            counts.update(dict(db.execute("SELECT CAST(route_id AS INT), count(*) FROM vehicle_feed "
                                          "WHERE rowid BETWEEN ? AND ? AND route_id>0 GROUP BY route_id",
                                          (row, row + 50000))))
    return [str(i) for i, _ in counts.most_common(limit)]


def gtfs_routes(path, selected):
    selected = set(selected)
    with zipfile.ZipFile(path) as feed:
        def rows(name):
            return csv.DictReader(io.TextIOWrapper(feed.open(name), encoding="utf-8-sig"))
        stop_coords = {r["stop_id"]: (float(r["stop_lat"]), float(r["stop_lon"])) for r in rows("stops.txt")}
        reference = {}
        for row in rows("trips.txt"):
            route, trip = row["route_id"], row["trip_id"]
            if route in selected and (route not in reference or
                                      "_FULL_" not in reference[route] and "_FULL_" in trip):
                reference[route] = trip
        selected_trips = {trip: route for route, trip in reference.items()}
        times = defaultdict(list)
        for row in rows("stop_times.txt"):
            route = selected_trips.get(row["trip_id"])
            if route:
                times[route].append((int(row["stop_sequence"]), row["stop_id"], clock(row["arrival_time"])))
    routes = {}
    for route, items in times.items():
        items.sort()
        stops = [v[1] for v in items]
        counts = Counter(stops)
        scheduled = [b[2] - a[2] for a, b in zip(items, items[1:])]
        coords = np.asarray([stop_coords[s] for s in stops])
        # Local equirectangular coordinates in metres, valid across Delhi.
        scale = np.asarray([111190.0, 111190.0 * np.cos(np.deg2rad(coords[:, 0].mean()))])
        routes[route] = (stops, scheduled, counts, cKDTree(coords * scale), scale)
    return routes


def window_events(frame, routes, midnight):
    passages = []
    segments = []
    for rid, group in frame.groupby("route", sort=False):
        route = str(int(rid))
        if route not in routes:
            continue
        stops, schedule, counts, tree, scale = routes[route]
        group = group.sort_values(["vehicle", "trip", "observed", "received"]).drop_duplicates(
            ["vehicle", "trip", "observed"])
        xy = group[["lat", "lng"]].to_numpy(dtype=float)
        distances, indices = tree.query(xy * scale)
        indices = np.where(distances <= RADIUS_M, indices, -1)
        observed = group.observed.to_numpy()
        vehicle = group.vehicle.to_numpy()
        trip = group.trip.to_numpy()
        same_run = (vehicle[1:] == vehicle[:-1]) & (trip[1:] == trip[:-1])
        gap = np.diff(observed)
        same_chunk = same_run & (gap > 0) & (gap <= FRESHNESS)
        chunk = np.cumsum(np.r_[True, ~same_chunk])
        hit = np.flatnonzero((indices[1:] >= 0) & (indices[1:] != indices[:-1]) & same_chunk) + 1
        received = group.received.to_numpy()
        last = None
        run = None
        for i in hit:
            idx = int(indices[i])
            if counts[stops[idx]] > 1:
                last = None
                continue
            if last is not None and chunk[i] == chunk[last] and idx == indices[last]:
                continue
            if last is None or vehicle[i] != vehicle[last] or trip[i] != trip[last]:
                token = f"{vehicle[i]}|{trip[i]}".encode()
                run = hashlib.blake2s(token, digest_size=8).hexdigest()
            at, seen = int(observed[i]), int(received[i])
            passages.append((seen, at, route, run, stops[idx]))
            if last is not None and chunk[last] == chunk[i] and idx == indices[last] + 1:
                duration = at - int(observed[last])
                scheduled = schedule[idx-1]
                if 5 <= scheduled <= 1800 and 5 <= duration <= 1200:
                    segments.append((max(seen, int(received[last])), at, route, stops[idx-1],
                                     stops[idx], duration, scheduled, (at-duration)-midnight, run))
            last = i
    return segments, passages


def extract(path, day, routes, windows=DEFAULT_WINDOWS):
    midnight = int(datetime.combine(date.fromisoformat(day), time(), IST).timestamp())
    selected = list(map(int, routes))
    binds = ",".join("?" for _ in selected)
    count = 0
    segments = []
    passages = []
    with open_db(path) as db:
        for start, end in windows:
            a, b = boundary(db, format_clock(start)), boundary(db, format_clock(end))
            query = f"""SELECT route_id AS route,vehicle_id AS vehicle,trip_id AS trip,
                        CAST(lat AS REAL) AS lat,CAST(lng AS REAL) AS lng,
                        CAST(timestamp AS INTEGER) AS observed, time AS seen
                        FROM vehicle_feed WHERE rowid>=? AND rowid<? AND route_id IN ({binds})
                        AND vehicle_id IS NOT NULL AND trip_id IS NOT NULL
                        AND time>=? AND time<?"""
            df = pd.read_sql_query(query, db, params=[a,b,*selected,format_clock(start),format_clock(end)])
            count += len(df)
            if df.empty:
                continue
            lookup = {t: clock(t) + midnight for t in df.seen.unique()}
            df["received"] = df.seen.map(lookup)
            # Exclude stale or future-clock reports and out-of-region coordinates.
            df = df[df.lat.between(28,30) & df.lng.between(76,78) &
                    (df.received - df.observed).between(0,FRESHNESS)]
            segs, events = window_events(df, routes, midnight)
            segments.extend(segs)
            passages.extend(events)
            print(day, format_clock(start), f"{len(segs):,} adjacent segments", flush=True)
    return segments, passages, count


def fit(segments):
    pairs = defaultdict(list)
    ratios = defaultdict(list)
    for _, _, route, start, end, secs, scheduled, second, _ in segments:
        tod = str(second % 86400 // BIN_SECONDS)
        key = "|".join((route,start,end))
        pairs[(key,"all")].append(secs)
        pairs[(key,tod)].append(secs)
        ratios[(route,"all")].append(secs / scheduled)
        ratios[(route,tod)].append(secs / scheduled)
    tracks = {}
    for (key, bucket), values in pairs.items():
        item = tracks.setdefault(key, [None, {}])
        estimate = [len(values),round(float(np.median(values)))]
        if bucket == "all":
            item[0] = estimate
        elif len(values) >= 2:
            item[1][bucket] = estimate
    route_info = {}
    for (route, bucket), values in ratios.items():
        if len(values) < 8:
            continue
        item = route_info.setdefault(route, [1,{}])
        value = round(float(np.clip(np.median(values),.4,2.5)),4)
        if bucket == "all":
            item[0] = value
        else:
            item[1][bucket] = value
    return {"pairs":tracks,"routes":route_info}


def metrics(actual, predicted):
    error = np.asarray(predicted) - np.asarray(actual)
    return {"n":len(error),"mae_s":round(float(np.abs(error).mean()),2),
            "p90_absolute_error_s":round(float(np.percentile(np.abs(error),90)),2),
            "signed_bias_s":round(float(error.mean()),2)}


def compare_priors(directory, other_path, gtfs_path):
    """Score two frozen priors on identical held-out segments common to both fits."""
    primary = EtaModel(read_data(directory / "eta_model.json.gz"))
    other = EtaModel(read_data(other_path))
    holdout = date.fromisoformat(primary.metadata["holdout"])
    for profile in (primary, other):
        if not profile.last_train_day or profile.last_train_day >= holdout:
            raise ValueError("Both historical training sets must end before the holdout")
    held = read_data(directory / "eta_replay.json.gz")["segments"]
    routes = gtfs_routes(gtfs_path, {r[2] for r in held})
    scheduled = {}
    for route, (stops, durations, _, _, _) in routes.items():
        for a, b, secs in zip(stops, stops[1:], durations):
            scheduled[(route,a,b)] = secs
    midnight = int(datetime.combine(holdout,time(),IST).timestamp())
    truth, primary_pred, other_pred, schedules = [], [], [], []
    pairs, common_routes = set(), set()
    for _, stop_time, route, a, b, actual in held:
        key = "|".join((route,a,b))
        if key not in primary.pairs or key not in other.pairs:
            continue
        if not midnight <= stop_time < midnight + 86400:
            raise ValueError("Replay observation falls outside the named holdout day")
        sched = scheduled[(route,a,b)]
        second = stop_time-actual-midnight
        truth.append(actual)
        schedules.append(sched)
        primary_pred.append(primary.duration(route,a,b,second,sched)[0])
        other_pred.append(other.duration(route,a,b,second,sched)[0])
        pairs.add(key)
        common_routes.add(route)
    if not truth:
        raise ValueError("The selected profiles have no shared held-out stop pairs")
    report = {"holdout":str(holdout),"primary_train":primary.metadata["train"],
              "comparison_train":other.metadata["train"],"all_held_out_segments":len(held),
              "common_held_out_segments":len(truth),"common_held_out_pairs":len(pairs),
              "common_held_out_routes":len(common_routes),"timetable":metrics(truth,schedules),
              "primary_historical":metrics(truth,primary_pred),
              "comparison_historical":metrics(truth,other_pred)}
    out = directory / "eta_day_type_comparison.json"
    out.write_text(json.dumps(report,indent=2),encoding="utf-8")
    print(json.dumps(report,indent=2))


def evaluate(segments, model):
    actual, scheduled, historic, recent = [], [], [], []
    bins = defaultdict(list)
    sources = Counter()
    runs = defaultdict(list)
    for seen, at, route, start, end, secs, sched, second, run in segments:
        est, _ = model.duration(route,start,end,second,sched)
        # Start of traversal is the forecast clock; future receipts are inaccessible.
        pred, source = model.duration(route,start,end,second,sched,at-secs)
        runs[(route,run)].append((at-secs, at, start, end, sched, second))
        actual.append(secs); scheduled.append(sched); historic.append(est); recent.append(pred)
        bins[f"{second//3600:02}"].append(len(actual)-1)
        sources[source] += 1
    result = {"timetable":metrics(actual,scheduled),"historical":metrics(actual,historic),
              "historical_recent":metrics(actual,recent),"sources":dict(sources),"hour_ist":{}}
    for key, ix in sorted(bins.items()):
        result["hour_ist"][key] = metrics(np.asarray(actual)[ix],np.asarray(recent)[ix])
    # Contiguous downstream horizons. Lock as-of across ALL legs of each prediction.
    horizons = {}
    for length in (3,5,10):
        target, scheds, preds, histories = [], [], [], []
        for (route,_), edges in runs.items():
            edges.sort()
            for pos in range(len(edges)-length+1):
                window = edges[pos:pos+length]
                if any(i[1] != j[0] or i[3] != j[2] for i,j in zip(window,window[1:])):
                    continue
                departure, second = window[0][0], window[0][5]
                elapsed = prior = 0
                for _,_,start,end,sched,_ in window:
                    elapsed += model.duration(route,start,end,second + elapsed,sched,departure)[0]
                    prior += model.duration(route,start,end,second + prior,sched)[0]
                target.append(window[-1][1]-departure); scheds.append(sum(x[4] for x in window))
                preds.append(elapsed); histories.append(prior)
        if target:
            horizons[str(length)] = {"timetable":metrics(target,scheds),
                                      "historical":metrics(target,histories),
                                      "historical_recent":metrics(target,preds)}
            # Passenger is ready at the target stop six minutes after the query.
            # This is a sensitivity check on bus arrival, not measured rider behaviour.
            ready = 360
            truth = np.asarray(target) >= ready
            predicted = np.asarray(preds) >= ready
            claimed = int(predicted.sum())
            false_catches = int((predicted & ~truth).sum())
            horizons[str(length)]["catchability_360s"] = {
                "evaluated":len(target),"predicted_catchable":claimed,
                "predicted_catchable_but_bus_was_early":false_catches,
                "false_catch_share_of_recommendations":round(false_catches/claimed,4) if claimed else None,
                "missed_feasible_options":int((~predicted & truth).sum())}
    result["downstream_stops"] = horizons
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-dir",type=Path)
    parser.add_argument("--gtfs",type=Path,default=Path("delhi_buses_static_gtfs_v1.zip"))
    parser.add_argument("--data-dir",type=Path,default=Path("data"))
    parser.add_argument("--train",nargs="+",default=["2026-10-12","2026-10-13"])
    parser.add_argument("--holdout",default="2026-10-15")
    parser.add_argument("--routes",type=int,default=160)
    parser.add_argument("--late-evening",action="store_true",
                        help="Also sample 20:00 through 21:30 IST on every selected day.")
    parser.add_argument("--compare-prior",type=Path,
                        help="Compare a second frozen model to the existing fit on common holdout pairs.")
    parser.add_argument("--evaluate-only",action="store_true",
                        help="Recompute holdout metrics for the already fitted model.")
    args = parser.parse_args()
    if args.compare_prior:
        compare_priors(args.data_dir,args.compare_prior,args.gtfs)
        return
    if args.db_dir is None:
        parser.error("--db-dir is required to fit or evaluate a model")
    if max(args.train) >= args.holdout or len(set(args.train)) != len(args.train):
        parser.error("Train dates must be unique and earlier than holdout.")
    def db(day):
        return args.db_dir / f"bus_movements_{day.replace('-','_')}.db"
    windows = [*DEFAULT_WINDOWS, (20 * 3600,21 * 3600 + 1800)] if args.late_evening else DEFAULT_WINDOWS
    window_labels = [[format_clock(a),format_clock(b)] for a,b in windows]
    selected = candidate_routes(db(min(args.train)),args.routes,windows)
    routes = gtfs_routes(args.gtfs,selected)
    if args.evaluate_only:
        fitted = read_data(args.data_dir / "eta_model.json.gz")
        meta = fitted["metadata"]
        if (meta["train"], meta["holdout"], meta["requested_routes"]) != (
                args.train, args.holdout, args.routes):
            parser.error("Evaluation arguments must match the existing fit.")
        if meta["windows_ist"] != window_labels:
            parser.error("Evaluation windows must match the existing fit.")
        validation, events, _ = extract(db(args.holdout),args.holdout,routes,windows)
        replay = {"segments":[s[:6] for s in validation],"passages":events}
        report = {**meta,"holdout_metrics":evaluate(validation,EtaModel(fitted,replay))}
        (args.data_dir / "eta_metrics.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
        print(json.dumps(report,indent=2))
        return
    history = []
    coverage = {}
    for day in [*args.train,args.holdout]:
        segs, events, count = extract(db(day),day,routes,windows)
        coverage[day] = {"selected_raw_pings":count,"passages":len(events),"segments":len(segs),
                         "routes_with_segments":len({s[2] for s in segs})}
        if not segs:
            raise RuntimeError(f"No labelled journeys on {day}; inspect the archive and feed.")
        if day in args.train:
            history.extend(segs)
        else:
            validation = segs
            replay = {"segments":[s[:6] for s in segs],"passages":events}
    fitted = fit(history)
    meta = {"train":args.train,"holdout":args.holdout,"requested_routes":args.routes,
            "selected_gtfs_routes":len(routes),"windows_ist":window_labels,
            "coverage":coverage,"historical_pairs":len(fitted["pairs"])}
    fitted["metadata"] = meta
    store(args.data_dir / "eta_model.json.gz",fitted)
    store(args.data_dir / "eta_replay.json.gz",replay)
    report = {**meta,"holdout_metrics":evaluate(validation,EtaModel(fitted,replay))}
    (args.data_dir / "eta_metrics.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
    print(json.dumps(report,indent=2))


if __name__ == "__main__":
    main()
