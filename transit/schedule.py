"""Build a read-only, versioned cache of the source GTFS schedule."""

import csv
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import zipfile
from array import array
from collections import defaultdict
from contextlib import closing
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=110000)
def clock_seconds(value):
    hours, minutes, seconds = map(int, value.split(":"))
    return 3600 * hours + 60 * minutes + seconds


def rows(feed, name):
    path = next((n for n in feed.namelist() if Path(n).name == name and "__MACOSX" not in n), None)
    if path is None:
        return
    with feed.open(path) as source, io.TextIOWrapper(source, encoding="utf-8-sig", newline="") as stream:
        yield from csv.DictReader(stream)


def load_groups(feed):
    trips = {r["trip_id"]: (r["route_id"], r["service_id"], r.get("shape_id", ""))
             for r in rows(feed, "trips.txt")}
    stops = {r["stop_id"]: r for r in rows(feed, "stops.txt")}
    stop_ids = list(stops)
    stop_index = {s: i for i, s in enumerate(stop_ids)}
    calls = defaultdict(lambda: array("i"))
    for r in rows(feed, "stop_times.txt"):
        arrival = r["arrival_time"] or r["departure_time"]
        departure = r["departure_time"] or arrival
        if r["trip_id"] not in trips or r["stop_id"] not in stop_index or not arrival:
            continue
        calls[r["trip_id"]].extend((int(r["stop_sequence"]), stop_index[r["stop_id"]],
            clock_seconds(arrival), clock_seconds(departure),
            int(r.get("pickup_type") or 0), int(r.get("drop_off_type") or 0)))
    grouped = defaultdict(list)
    for trip, records in calls.items():
        it = iter(records)
        ordered = sorted(zip(it, it, it, it, it, it))
        if len(ordered) < 2:
            continue
        sequence, places, arrivals, departures, pickups, drops = zip(*ordered)
        base = arrivals[0]
        if any(a > d for a, d in zip(arrivals, departures)) or any(
                d > a for d, a in zip(departures, arrivals[1:])):
            continue
        key = (*trips[trip], tuple(stop_ids[i] for i in places),
               tuple(a-base for a in arrivals), tuple(d-base for d in departures), pickups, drops, sequence)
        grouped[key].append((base, trip))
    return stops, grouped


def build(feed_path, target):
    with zipfile.ZipFile(feed_path) as feed, closing(sqlite3.connect(target)) as db, db:
        db.executescript("""
            CREATE TABLE metadata (name TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE patterns (route TEXT, service TEXT, shape TEXT, stops TEXT,
                arrivals TEXT, departures TEXT, pickups TEXT, drops TEXT, sequences TEXT, trips TEXT);
            CREATE TABLE shapes (id TEXT PRIMARY KEY, points TEXT);
        """)
        stops, groups = load_groups(feed)
        tables = {
            "stops": [(k, r["stop_name"], float(r["stop_lat"]), float(r["stop_lon"]))
                      for k, r in stops.items() if r.get("stop_lat") and r.get("stop_lon")],
            "routes": [(r["route_id"], r.get("route_short_name") or r.get("route_long_name")
                       or r["route_id"], r.get("agency_id", "")) for r in rows(feed, "routes.txt")],
            "calendar": list(rows(feed, "calendar.txt")),
            "exceptions": list(rows(feed, "calendar_dates.txt")),
            "feed": list(rows(feed, "feed_info.txt")),
        }
        db.executemany("INSERT INTO metadata VALUES (?,?)", ((k, json.dumps(v)) for k,v in tables.items()))
        for (route, service, shape, stops, arrivals, departures, pickups, drops, sequences), trips in groups.items():
            db.execute("INSERT INTO patterns VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (route, service, shape, *(json.dumps(v, separators=(",", ":")) for v in
                        (stops, arrivals, departures, pickups, drops, sequences, sorted(trips)))))
        shape_points = defaultdict(list)
        for r in rows(feed, "shapes.txt"):
            shape_points[r["shape_id"]].append((int(r["shape_pt_sequence"]), float(r["shape_pt_lat"]),
                                                float(r["shape_pt_lon"])))
        for shape, points in shape_points.items():
            db.execute("INSERT INTO shapes VALUES (?,?)",
                       (shape, json.dumps([(a,b) for _,a,b in sorted(points)], separators=(",", ":"))))


def portable_cache_name(feed_path):
    """Identify the exact timetable even when copying/unzipping changes its timestamp."""
    with Path(feed_path).open("rb") as source:
        checksum = hashlib.file_digest(source, "sha256").hexdigest()
    return f"router-v2-sha256-{checksum}.sqlite"


def cache_schedule(feed_path, data_dir):
    feed_path, data_dir = Path(feed_path), Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    stat = feed_path.stat()
    target = data_dir / portable_cache_name(feed_path)
    if target.exists():
        return target
    legacy = data_dir / f"router-v2-{stat.st_size}-{stat.st_mtime_ns}.sqlite"
    if legacy.exists():
        return legacy
    handle, temp = tempfile.mkstemp(prefix="router-build-", suffix=".sqlite", dir=data_dir)
    os.close(handle)
    try:
        build(feed_path, temp)
        if not target.exists():
            os.replace(temp, target)
    finally:
        Path(temp).unlink(missing_ok=True)
    return target
