"""Screen every extracted route-hour case and pair full journey searches on a fixed sample."""

import argparse
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from functools import lru_cache
import gzip
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from transit.router import IST
from tools.fleet_positions import received_positions

BASELINE = ROOT / "docs/october-v041-policy-source.zip"
CASE_DIR = ROOT / "data/fleet-cases"
OUTPUT = ROOT / "data/fleet-router"
SALT = "delhi-fleet-boarding-route-civil-hour-v1"
LABELS = {"approaching", "gone", "uncertain", "unverified"}


def identity(row):
    return row["day"], str(row["route_id"]), row["hour"]


def rank(row):
    parts = [SALT, *identity(row), row["ready"], row["stop"]]
    return hashlib.sha256("|".join(map(str, parts)).encode()).digest()


def stratify(rows, names):
    """Choose without consulting labels, policy decisions or post-readiness observations."""
    best = {}
    for row in rows:
        if row["eligibility"]:
            continue
        day, route, hour = identity(row)
        keys = [("route", route, hour < 6), ("date_hour", day, hour)]
        if names.get(route) in ("502UP", "502DOWN"):
            keys.append(("diagnostic", route, hour))
        score = rank(row)
        for key in keys:
            if key not in best or score < best[key][0]:
                best[key] = score, identity(row)
    return {key for _, key in best.values()}


class TimetableMatcher:
    def __init__(self, router, registry):
        self.router = router
        self.registry = registry

    @lru_cache(maxsize=120000)
    def variants(self, route, minute, upstream, stop, following):
        # A provider wall clock may be encoded in GTFS as 00:xx or 24:xx.
        # An explicitly extended provider clock retains its service-day offset.
        minutes = (minute, minute + 1440) if minute < 1440 else (minute,)
        variants, found_clock = [], False
        for start in minutes:
            for pid, k in self.registry.by_clock.get((route, start), ()):
                found_clock = True
                p = self.router.patterns[pid]
                for i in range(1, len(p.stops) - 1):
                    if p.stops[i - 1:i + 2] == [upstream, stop, following]:
                        variants.append((p.service, p.starts[k] + p.departures[i], p.trips[k],
                                         bool(p.pickups[i] or p.drops[i + 1])))
        return found_clock, variants

    def match(self, row):
        if row.get("identity_reason"):
            return row["identity_reason"], None, None
        minute = row.get("start_minute")
        if minute is None or minute < 0:
            return "unrecognized_trip_clock", None, None
        found, variants = self.variants(str(row["route_id"]), minute, row["upstream"], row["stop"], row["next_stop"])
        if not found:
            return "no_timetable_start_clock", None, None
        if not variants:
            return "ordered_stop_pattern_mismatch", None, None
        day = date.fromisoformat(row["day"])
        midnight = int(datetime.combine(day, time(), IST).timestamp())
        dated = []
        for delta in (-1, 0, 1):
            service_day = day + timedelta(days=delta)
            active = self.router._active(service_day)
            for service, seconds, trip, restricted in variants:
                if service in active and abs(midnight + delta * 86400 + seconds - row["forecast"]) <= 5400:
                    dated.append((service_day.isoformat(), trip, restricted))
        if not dated:
            return "no_dated_call_within_90_minutes", None, None
        if len(dated) != 1:
            return "ambiguous_dated_trip_or_pattern", None, None
        service_day, trip, restricted = dated[0]
        if restricted:
            return "boarding_or_alighting_restricted", None, None
        return None, service_day, trip


@contextmanager
def baseline_package():
    # Load two independent module namespaces. Static data, dates and protobuf inputs agree.
    with tempfile.TemporaryDirectory(prefix="delhi-router-v041-") as folder:
        package = Path(folder) / "atlas_v041"
        package.mkdir()
        with zipfile.ZipFile(BASELINE) as archive:
            for member in archive.namelist():
                path = Path(member)
                if path.parent.as_posix() == "transit" and path.suffix == ".py":
                    (package / path.name).write_bytes(archive.read(member))
        name = "atlas_v041"
        spec = importlib.util.spec_from_file_location(name, package / "__init__.py",
                                                       submodule_search_locations=[str(package)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        try:
            yield name
        finally:
            for key in tuple(sys.modules):
                if key == name or key.startswith(name + "."):
                    del sys.modules[key]


class Policy:
    def __init__(self, package, shared_walking=None):
        self.data = ROOT / "data"
        eta = importlib.import_module(package + ".eta")
        self.model = eta.EtaModel(eta.read_data(self.data / "eta_model.json.gz"))
        self.model.weekend = eta.EtaModel(eta.read_data(self.data / "weekend/eta_model.json.gz"))
        walking_module = importlib.import_module(package + ".walking")
        if shared_walking and Path(walking_module.__file__).read_bytes() == (ROOT / "transit/walking.py").read_bytes():
            walking = shared_walking
        else:
            walking = walking_module.Walking(self.data)
        self.router = importlib.import_module(package + ".router").Router(
            ROOT / "delhi_buses_static_gtfs_v1.zip", self.data, walking, self.model)
        self.overlay_type = importlib.import_module(package + ".replay").CapturedOverlay
        self.registry = self.overlay_type(self.model, self.data, 0).bind(self.router)
        self.feed_type = importlib.import_module(package + ".live").protobuf().FeedMessage

    def feed(self, receipt):
        feed = self.feed_type()
        feed.header.gtfs_realtime_version = "2.0"
        feed.header.timestamp = receipt
        return feed

    def run(self, row, service_day, trip):
        issue = row["ready"]
        overlay = self.overlay_type(self.model, self.data, issue)
        for field in ("router", "ids", "by_clock", "edges"):
            setattr(overlay, field, getattr(self.registry, field))

        def identify(entity):
            entity.vehicle.id = row["bus"]
            entity.trip.route_id = str(row["route_id"])
            entity.trip.trip_id = trip
            entity.trip.start_date = service_day.replace("-", "")

        receipt = row["forecast_receipt"]
        feed = self.feed(receipt)
        update = feed.entity.add(id="reconstruction").trip_update
        identify(update)
        update.timestamp = receipt
        update.stop_time_update.add(stop_id=row["stop"]).departure.time = row["forecast"]
        overlay.apply_trip_updates(feed, receipt)
        for received,positions in received_positions(row["observations"]):
            feed = self.feed(received)
            for i,(observed,lat,lon) in enumerate(positions):
                if not observed <= received <= issue:
                    raise ValueError("Post-issue evidence supplied to the planner")
                vehicle = feed.entity.add(id="archived-"+str(i)).vehicle
                identify(vehicle)
                vehicle.timestamp = observed
                vehicle.position.latitude, vehicle.position.longitude = lat, lon
            overlay.apply_positions(feed, received, provider="community_positions")
        asof = datetime.fromtimestamp(issue, IST)
        accepted = overlay.departure(trip, row["stop"], date.fromisoformat(service_day), row["forecast"], issue)
        result = self.router.journeys(row["stop"], row["next_stop"], asof, asof=asof, eta=overlay)
        strict = result["journeys"][0]["duration_seconds"] if result["journeys"] else None
        alerts = [a for a in result["boarding_alerts"] if a["trip_id"] == trip and a["platform"]["id"] == row["stop"]]
        target = next((a for a in alerts if a["alighting_stop"]["id"] == row["next_stop"]), None)
        offered = bool(target and target.get("conditional_itinerary"))
        if offered:
            assert not target["catchable"] and target["seconds_before_ready"] == row["gap"] and target["fallback_itinerary"]
            reason = "conditional_with_same_platform_fallback"
        elif accepted is None:
            reason = "reconstructed_call_unavailable_at_issue"
        elif strict is None:
            reason = "no_ordinary_bus_journey"
        elif target:
            reason = "no_same_platform_fallback"
        elif alerts:
            reason = "different_alighting_stop_selected"
        elif strict <= 300:
            reason = "ordinary_transit_within_five_minutes"
        else:
            reason = "no_qualifying_direct_alert"
        return {"offered": offered, "reason": reason, "trip": trip, "service_day": service_day,
                "accepted": accepted is not None, "strict_seconds": strict,
                "conditional_seconds": target["conditional_itinerary"]["duration_seconds"] if offered else None,
                "same_platform_fallback_seconds": target["fallback_itinerary"]["duration_seconds"] if offered else None,
                "gps_age_seconds": target.get("gps_age_seconds") if target else None,
                "vehicle_approaching": target.get("vehicle_approaching") if target else None}


def read_day(day, manifest):
    with gzip.open(CASE_DIR / (day + ".json.gz"), "rt", encoding="utf-8") as stream:
        payload = json.load(stream)
    if (payload["day"] != day or not payload["complete"] or len(payload["hours"]) != 24
            or payload["protocol"] != manifest["protocol"] or payload["signature"] != manifest["signature"]):
        raise ValueError("Incomplete or incompatible daily GPS case file: " + day)
    return payload["cases"]


def dump(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".pending")
    with temp.open("wb") as dest, gzip.GzipFile(fileobj=dest, mode="wb", mtime=0, filename="") as gz:
        gz.write(json.dumps(payload, allow_nan=False, separators=(",", ":")).encode())
    temp.replace(path)


def fingerprint():
    names = ("live", "router", "boarding", "arrivals", "eta", "walking", "schedule", "replay", "contract")
    return {name: hashlib.sha256((ROOT / ("transit/" + name + ".py")).read_bytes()).hexdigest() for name in names}


def daily_entries(day, manifest, matcher):
    seen = set()
    for row in read_day(day, manifest):
        ready = datetime.fromtimestamp(row["ready"], IST)
        if (ready.date().isoformat() != day or row["hour"] != ready.hour or row["outcome"] not in LABELS
                or row["forecast"] + row["gap"] != row["ready"] or not 1 <= row["gap"] <= 30):
            raise ValueError("Inconsistent route-hour candidate")
        entry = {key:row[key] for key in ("day","route_id","ready","stop","outcome","gap","hour")}
        entry["route_id"] = str(entry["route_id"])
        entry["eligibility"],entry["service_day"],entry["trip"] = matcher.match(row)
        if bool(row["eligible_named_case"]) != (entry["eligibility"] is None):
            raise ValueError("Collector and independent dated-match check disagree")
        key = identity(entry)
        if key in seen:
            raise ValueError("Duplicate route/date/hour in extracted candidates")
        seen.add(key)
        yield row,entry


@contextmanager
def output_days(path, metadata):
    if path is None:
        yield lambda rows:None
        return
    pending = path.with_suffix(path.suffix+".pending")
    with pending.open("wb") as raw, gzip.GzipFile(fileobj=raw,mode="wb",filename="",mtime=0) as archive:
        archive.write(b'{"cases":[')
        written = 0
        def write(rows):
            nonlocal written
            if not rows:
                return
            if written:
                archive.write(b',')
            archive.write(json.dumps(rows,allow_nan=False,separators=(",",":"))[1:-1].encode())
            written += len(rows)
        yield write
        if written != metadata["screened"]:
            raise ValueError("Paired output has the wrong number of GPS cases")
        archive.write(b'],"selection":' + json.dumps(metadata,separators=(",",":")).encode() + b'}')
    pending.replace(path)


def run(split, pilot_day=None, shard=0, workers=1):
    full_dates = [f"2026-10-{n:02}" for n in (range(1, 24) if split == "dev" else range(24, 32))]
    if pilot_day and (split != "dev" or pilot_day not in full_dates):
        raise ValueError("The pilot is restricted to development days")
    if workers < 1 or not 0 <= shard < workers:
        raise ValueError("Worker index is outside the selected partition")
    dates = [pilot_day] if pilot_day else full_dates
    fingerprints = fingerprint()
    if fingerprints != json.loads((ROOT/"docs/fleet-policy-freeze.json").read_text()):
        raise ValueError("Production source differs from the policy frozen for this comparison")
    code = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    old_digest = hashlib.sha256(BASELINE.read_bytes()).hexdigest()
    study_raw = (CASE_DIR/"manifest.json").read_bytes()
    study_digest = hashlib.sha256(study_raw).hexdigest()
    manifest = json.loads(study_raw)
    input_names = ("data/eta_model.json.gz", "data/weekend/eta_model.json.gz", "data/walk.npz",
                   "data/walk-meta.json", "delhi_buses_static_gtfs_v1.zip", "tools/fleet_positions.py")
    input_hashes = {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in input_names}
    signature = hashlib.sha256(json.dumps([fingerprints,code,old_digest,study_digest,input_hashes], sort_keys=True).encode()).hexdigest()
    revised = Policy("transit")
    matcher = TimetableMatcher(revised.router, revised.registry)
    screened = 0
    pilot_winners = {}
    def summaries():
        nonlocal screened
        for day in dates:
            for _,entry in daily_entries(day,manifest,matcher):
                screened += 1
                if pilot_day and not entry["eligibility"]:
                    score,hour = rank(entry),entry["hour"]
                    if hour not in pilot_winners or score < pilot_winners[hour][0]:
                        pilot_winners[hour] = score,identity(entry)
                yield entry
    chosen = stratify(summaries(), {key:value[0] for key,value in revised.router.routes.items()})
    if pilot_day:
        chosen = {key for _,key in pilot_winners.values()}
    print(split,screened,"screened",len(chosen),"paired searches",flush=True)
    metadata = {"salt":SALT,"split":split,"dates":dates,"replayed":len(chosen),"screened":screened,
                "revised_source_sha256":fingerprints,"study_manifest_sha256":study_digest,
                "evaluator_sha256":code,"baseline_bundle_sha256":old_digest,"network_and_model_sha256":input_hashes}
    path = OUTPUT/(split+".json.gz") if not pilot_day and workers==1 else None
    OUTPUT.mkdir(parents=True, exist_ok=True)
    cache = sqlite3.connect(OUTPUT / ".paired-cache.sqlite", timeout=90)
    cache.execute("PRAGMA journal_mode=WAL")
    cache.execute("CREATE TABLE IF NOT EXISTS results (signature TEXT, sample TEXT, payload TEXT,"
                  "PRIMARY KEY(signature,sample))")
    with cache, baseline_package() as package, output_days(path,metadata) as write:
        old = Policy(package, shared_walking=revised.router.walking)
        for day in dates:
            computed = 0
            entries = []
            for row,entry in daily_entries(day,manifest,matcher):
                entry["selected"] = identity(entry) in chosen
                entries.append(entry)
                if not entry["selected"] or int.from_bytes(rank(entry)[:4],"big") % workers != shard:
                    continue
                sample = hashlib.sha256(json.dumps(row,sort_keys=True,separators=(",",":")).encode()).hexdigest()
                saved = cache.execute("SELECT payload FROM results WHERE signature=? AND sample=?", (signature,sample)).fetchone()
                if saved:
                    entry.update(json.loads(saved[0]))
                else:
                    paired = {label: engine.run(row,entry["service_day"],entry["trip"])
                              for label,engine in (("baseline",old),("revised",revised))}
                    cache.execute("INSERT OR IGNORE INTO results VALUES (?,?,?)", (signature,sample,json.dumps(paired)))
                    cache.commit()
                    entry.update(paired)
                    computed += 1
            if path and any(("baseline" in entry and "revised" in entry)!=entry["selected"] for entry in entries):
                raise ValueError("Incomplete paired production comparison")
            write(entries)
            print(split,day,"worker",shard,"new pairs",computed,flush=True)
        if fingerprint()!=fingerprints:
            raise RuntimeError("Production policy changed during the replay")
    cache.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("split", choices=("dev", "holdout"))
    parser.add_argument("--pilot-day", help="Cache the per-hour winners on one development date")
    parser.add_argument("--workers", type=int, default=1, help="Partition full searches across this many processes")
    parser.add_argument("--shard", type=int, default=0, help="Zero-based partition to warm; run once unpartitioned to export")
    args = parser.parse_args()
    run(args.split, args.pilot_day, args.shard, args.workers)
