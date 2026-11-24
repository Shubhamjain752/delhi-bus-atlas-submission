"""Compare the deployed and expanded October historical ETA profiles on identical held-out bus movements.

Requires the 31 anonymous daily observation files produced by the October extractor.
Writes two candidate profiles and a comparison report into a new directory. The
existing planner data stays in place. All times and split dates refer to Delhi.
"""
import argparse
from collections import Counter, defaultdict
from contextlib import ExitStack
import csv
from datetime import date, datetime, timedelta
import gzip
from hashlib import file_digest
import io
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from zipfile import ZipFile

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from transit.eta import BIN_SECONDS, EtaModel, IST, read_data


FIRST = date(2026, 10, 1)
DAYS = [FIRST + timedelta(days=i) for i in range(31)]
START = int(datetime(2026, 10, 1, tzinfo=IST).timestamp())
SPLIT = int(datetime(2026, 10, 25, tzinfo=IST).timestamp())
END = int(datetime(2026, 11, 1, tzinfo=IST).timestamp())
MODELS = ("timetable", "existing", "candidate")
FOLD = ("fit-01-24", "november-forward")
PROFILES = ("weekday", "weekend")
HISTORY = ("timetable", "route", "route_time_band", "stop_pair", "stop_pair_time_band")
COHORT = ("route_without_existing_history", "existing_route_only", "existing_stop_pair")
SHARDS = 128
LABEL = np.dtype([("route", "<i4"), ("from_stop", "<i4"), ("to_stop", "<i4"), ("day", "u1"),
                  ("hour", "u1"), ("bin", "u1"), ("seconds", "<u2"), ("scheduled", "<u2"), ("flags", "u1")])
COLUMNS = ("route", "from_stop", "to_stop", "start", "end", "issue_received", "received", "seconds", "scheduled")


def sha(path):
    with path.open("rb") as file:
        return file_digest(file, "sha256").hexdigest()


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def label_shards(labels, census_dir, working):
    provenance, counts = {}, Counter()
    by_start_day = {str(day): Counter() for day in DAYS}
    all_routes = set()
    with ExitStack() as files:
        streams = [files.enter_context((working / f"{i}.bin").open("wb")) for i in range(SHARDS)]
        for index, day in enumerate(DAYS):
            path = labels / (day.isoformat() + ".npz")
            receipt = path.with_suffix(".json")
            metadata = json.loads(receipt.read_bytes())
            if census_dir.is_dir():
                with gzip.open(census_dir / (day.isoformat() + ".json.gz"), "rt", encoding="utf-8") as stream:
                    independent = json.load(stream)
            else:
                from tools.october_census import read_census
                independent = read_census(str(day), source=census_dir)
            observed_hours = {str(h).zfill(2): int(v) for h, v in metadata["hourly_raw"].items() if v}
            if (metadata["method"] != "delhi-directed-traversals-v1" or metadata["day"] != day.isoformat()
                    or not independent["complete"] or independent["day"] != day.isoformat()
                    or metadata["source_member_crc32"] != independent["archive_member"]["crc32"]
                    or metadata["raw"] != independent["census"]["raw_records"]
                    or observed_hours != independent["census"]["by_hour"]):
                raise ValueError(f"{day}: daily receipt does not match the independent October census")
            with np.load(path, allow_pickle=False) as daily:
                arrays = {key: daily[key] for key in COLUMNS}
            n = len(arrays["route"])
            if metadata["labels"] != n or metadata["routes_with_labels"] != len(np.unique(arrays["route"])):
                raise ValueError(f"{day}: incomplete label export")
            if any(col.shape != (n,) or col.dtype != np.int32 for col in arrays.values()):
                raise ValueError(f"{day}: expected one equally sized int32 column per label field")
            start, end = arrays["start"], arrays["end"]
            first_receipt, last_receipt = arrays["issue_received"], arrays["received"]
            valid = ((end - START) // 86400 == index) & (end - start == arrays["seconds"])
            valid &= (arrays["seconds"] >= 5) & (arrays["seconds"] <= 1200)
            valid &= (arrays["scheduled"] >= 5) & (arrays["scheduled"] <= 1800)
            valid &= (first_receipt >= start) & (first_receipt <= start + 90)
            valid &= (last_receipt >= first_receipt) & (last_receipt >= end) & (last_receipt <= end + 90)
            if not valid.all():
                raise ValueError(f"{day}: {n - int(valid.sum())} labels failed the clock or duration contract")
            digest = sha(path)
            provenance[day.isoformat()] = {"labels_by_downstream_day": n, "npz_sha256": digest,
                                          "receipt_sha256": sha(receipt), "archive_member_crc32": metadata["source_member_crc32"],
                                          "raw_gps_records": metadata["raw"], "fresh_identified_positions": metadata["fresh_identified_positions"],
                                          "routes_with_labels": metadata["routes_with_labels"]}
            keep = (start >= START) & (start < END)
            timely = (first_receipt < SPLIT) & (last_receipt < SPLIT)
            # A delayed report between stops can affect whether a track was continuous.
            # Let the full 90-second accepted lateness window elapse before either fit.
            train = keep & (start < SPLIT) & timely & (end + 90 < SPLIT)
            test = keep & (start >= SPLIT)
            forward = keep & (first_receipt < END) & (last_receipt < END) & (end + 90 < END)
            counts.update({"all_extracted": n, "starts_before_october": int((start < START).sum()),
                           "training": int(train.sum()), "holdout": int(test.sum()), "forward": int(forward.sum()),
                           "pre_split_but_received_later": int((keep & (start < SPLIT) & ~timely).sum()),
                           "pre_split_received_but_not_mature": int((keep & (start < SPLIT) & timely & ~train).sum()),
                           "forward_cutoff_exclusions": int((keep & ~forward).sum())})
            rows = np.empty(n, dtype=LABEL)
            for key in ("route", "from_stop", "to_stop", "seconds", "scheduled"):
                rows[key] = arrays[key]
            local = start - START
            rows["day"] = local // 86400 + 1
            rows["hour"] = local % 86400 // 3600
            rows["bin"] = local % 86400 // BIN_SECONDS
            rows["flags"] = train.astype("u1") + 2 * forward.astype("u1") + 4 * test.astype("u1")
            rows = rows[train | test | forward]
            all_routes.update(map(int, np.unique(rows["route"])))
            for flag, name in ((1, "fit_1_to_24"), (2, "forward_fit"), (4, "holdout")):
                amounts = np.bincount(rows["day"][(rows["flags"] & flag) != 0], minlength=32)
                for d in DAYS:
                    by_start_day[str(d)][name] += int(amounts[d.day])
            order = np.argsort(rows["route"] % SHARDS, kind="stable")
            ordered = rows[order]
            bucket = ordered["route"] % SHARDS
            offsets = np.flatnonzero(bucket[1:] != bucket[:-1]) + 1
            for batch in np.split(ordered, offsets):
                if batch.size:
                    batch.tofile(streams[int(batch["route"][0]) % SHARDS])
            print(f"{day}: {n:,} labels", flush=True)
    counts["distinct_routes_with_eligible_labels"] = len(all_routes)
    return provenance, dict(counts), by_start_day


def fit(rows):
    """The count, median, clip and sparse-cell thresholds from tools/train_eta.fit."""
    pairs, routes = {}, {}
    if not len(rows):
        return {"pairs": pairs, "routes": routes}
    frame = pd.DataFrame.from_records(rows, columns=rows.dtype.names)
    frame["ratio"] = frame["seconds"] / frame["scheduled"]
    pair_columns = ["route", "from_stop", "to_stop"]
    for by_band in (False, True):
        band_column = ["bin"] if by_band else []
        medians = frame.groupby(pair_columns + band_column, sort=False)["seconds"].agg(["size", "median"])
        for key, count, median in medians.itertuples():
            route, start, end = key[:3]
            name = f"{route}|{start}|{end}"
            cell = [int(count), round(float(median))]
            if by_band:
                if count >= 2:
                    pairs[name][1][str(key[-1])] = cell
            else:
                pairs[name] = [cell, {}]
        medians = frame.groupby(["route"] + band_column, sort=False)["ratio"].agg(["size", "median"])
        for key, count, median in medians.itertuples():
            if count < 8:
                continue
            value = round(float(np.clip(median, .4, 2.5)), 4)
            if by_band:
                routes[str(key[0])][1][str(key[-1])] = value
            else:
                routes[str(key)] = [value, {}]
    return {"pairs": pairs, "routes": routes}


def predict(rows, model):
    fields = ["route", "from_stop", "to_stop", "bin", "scheduled", "day"]
    distinct, back = np.unique(rows[fields], return_inverse=True)
    predictions = np.empty(len(distinct), dtype=np.int32)
    sources = np.zeros(len(distinct), dtype=np.uint8)
    for i, (route, start, end, band, sched, day) in enumerate(distinct):
        r, s, e, b = (str(int(v)) for v in (route, start, end, band))
        predictions[i], source = model.duration(r, s, e, int(band) * BIN_SECONDS, int(sched),
                                               asof=None, service_date=DAYS[int(day)-1])
        pair = model.pairs.get("|".join((r, s, e)))
        ratio = model.route_ratios.get(r)
        if source == "gps_historical":
            sources[i] = (3 + (b in pair[1])) if pair else (1 + (b in ratio[1]))
        elif source != "timetable":
            raise AssertionError("A historical-only comparison used an unexpected prediction source")
    return predictions[back], sources[back]


class Metrics:
    def __init__(self):
        self.histogram = [np.zeros(1, dtype=np.int64) for _ in MODELS]
        self.signed = np.zeros(3, dtype=np.int64)
        self.absolute = np.zeros(3, dtype=np.int64)
        self.early = np.zeros(3, dtype=np.int64)
        self.closer = self.further = self.n = 0

    def add(self, errors):
        count = len(errors[0])
        if not count:
            return
        self.n += count
        absolute = np.abs(errors)
        self.closer += int((absolute[2] < absolute[1]).sum())
        self.further += int((absolute[2] > absolute[1]).sum())
        self.signed += errors.sum(axis=1, dtype=np.int64)
        self.absolute += absolute.sum(axis=1, dtype=np.int64)
        self.early += (errors < 0).sum(axis=1)
        for i, values in enumerate(absolute):
            histogram = np.bincount(values)
            previous = self.histogram[i]
            if histogram.size > previous.size:
                previous = self.histogram[i] = np.pad(previous, (0, histogram.size - previous.size))
            previous[:len(histogram)] += histogram

    def report(self):
        if not self.n:
            return {"n": 0}
        output = {"n": self.n, "candidate_closer": self.closer, "candidate_further": self.further,
                  "same_absolute_error": self.n - self.closer - self.further}
        # NumPy's linear percentile, calculated exactly from integer error frequencies.
        lower, fraction = divmod(9 * (self.n - 1), 10)
        for i, name in enumerate(MODELS):
            total = self.histogram[i].cumsum()
            low, high = total.searchsorted([lower + 1, min(lower + 2, self.n)])
            exact = int(self.histogram[i][0])
            output[name] = {"mae_seconds": round(float(self.absolute[i]) / self.n, 3),
                            "mae_total_seconds": int(self.absolute[i]),
                            "p90_absolute_error_seconds": round(float(low + (high - low) * fraction / 10), 2),
                            "signed_bias_seconds": round(float(self.signed[i]) / self.n, 3),
                            "within_30_seconds": int(self.histogram[i][:31].sum()),
                            "within_60_seconds": int(self.histogram[i][:61].sum()),
                            "early": int(self.early[i]), "exact": exact, "late": self.n - int(self.early[i]) - exact}
        return output


def grouped(bank, name, labels, errors):
    for key in np.unique(labels):
        bank[name, str(key)].add(errors[:, labels == key])


def score(rows, existing, candidate, region, bank, sources, per_route, route_names):
    if not len(rows):
        return
    old, old_sources = predict(rows, existing)
    new, new_sources = predict(rows, candidate)
    errors = np.stack((rows["scheduled"].astype("i4"), old, new)) - rows["seconds"].astype("i4")
    bank["overall", "all"].add(errors)
    bank["day_type", region].add(errors)
    for col in ("day", "hour"):
        grouped(bank, col, rows[col], errors)
    overnight = rows["hour"] < 5
    bank["time", "00:00–04:59"].add(errors[:, overnight])
    bank["time", "05:00–23:59"].add(errors[:, ~overnight])
    for label, array in (("existing", old_sources), ("candidate", new_sources)):
        sources[label] += np.bincount(array, minlength=len(HISTORY))
    # A route can have a trained stop pair even when its route-level ratio was too sparse.
    prior_routes = set(existing.route_ratios) | {k.split("|", 1)[0] for k in existing.pairs}
    on_old_route = np.isin(rows["route"], [int(r) for r in prior_routes])
    cohorts = np.where(old_sources >= 3, 2, on_old_route.astype("u1"))
    for index, cohort in enumerate(COHORT):
        bank["baseline_cohort", cohort].add(errors[:, cohorts == index])
    for route in np.unique(rows["route"]):
        mask = rows["route"] == route
        route_id = str(int(route))
        name = route_names[route_id]
        direction = "DOWN" if name.upper().endswith("DOWN") else "UP" if name.upper().endswith("UP") else "other"
        bank["direction", direction].add(errors[:, mask])
        per_route[route_id].add(errors[:, mask])


def merge_profile(parts, out, metadata):
    """Emit an EtaModel-compatible file without loading all route shards into memory."""
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=5, mtime=0) as zipped:
        def put(text):
            zipped.write(text.encode("utf-8"))
        put('{"metadata":' + json.dumps(metadata, separators=(",", ":")))
        for section in ("routes", "pairs"):
            put(',"' + section + '":{')
            first = True
            for path in parts:
                entries = json.loads(path.read_bytes())[section]
                if entries:
                    chunk = json.dumps(entries, separators=(",", ":"), allow_nan=False)[1:-1]
                    put(("" if first else ",") + chunk)
                    first = False
            put("}")
        put("}")


def compact_summary(result):
    def measures(row):
        if not row["n"]:
            return {"n": 0}
        return {"n": row["n"],
                **{key: row[key] for key in ("candidate_closer", "candidate_further", "same_absolute_error")},
                **{name: {key: row[name][key] for key in ("mae_seconds", "p90_absolute_error_seconds",
                                                          "signed_bias_seconds", "within_60_seconds")} for name in MODELS}}

    return {
        "question": "How much does a longer GPS history change the estimated time between two bus stops?",
        "test": "Same observed movements, same timetable durations, no recent GPS given to either historical model.",
        "fit": "1–24 October 2026, with separate weekday and weekend histories",
        "test_dates": "25–31 October 2026, including Sunday 25 and Saturday 31",
        "overnight": "00:00–04:59 IST. Both models use the date of the upstream GPS reading. The archive cannot identify the official service day for every bus after midnight; the live journey planner can keep the previous service day for some scheduled runs.",
        "limitations": "GPS stop-area passages measure bus movement. Walking, boarding, waiting and passenger arrival require separate checks. Results describe eligible continuously tracked stops within the reference route patterns.",
        "forward_profile": "Refitted on all 31 days for later journeys. October 25–31 cannot test that refit independently.",
        "metric_units": "Seconds. Positive bias means that the estimated ride between stops was longer than the measured ride. Closer/further compares absolute errors against the existing model.",
        "source": {"label_extraction": "tools/month_eta_labels.py", "candidate_fit_and_test": "tools/evaluate_october_eta.py",
                   "prediction": "transit/eta.py", "gtfs_sha256": result["gtfs_sha256"],
                   "evaluator_sha256": result["evaluator_sha256"], "label_method_sha256": result["label_protocol"]["matching_code_sha256"]},
        "baseline": result["baseline"], "fits": result["profiles"], "counts": result["counts"],
        "overall": result["overall"], "historical_coverage": result["historical_coverage"], "routes": result["routes"],
        "by_date": {k: measures(v) for k, v in result["by_date"].items()},
        "by_hour_ist": {k: measures(v) for k, v in result["by_hour_ist"].items()},
        "by_group": {g: {k: measures(v) for k, v in groups.items()} for g, groups in result["by_group"].items()},
    }


def run(args, working, output):
    protocol = json.loads((args.labels / "protocol.json").read_bytes())
    gtfs_sha = sha(args.gtfs)
    if (protocol["method"] != "delhi-directed-traversals-v1" or tuple(protocol["columns"]) != COLUMNS
            or protocol["official_gtfs_sha256"] != gtfs_sha
            or protocol["reference_trainer_sha256"] != sha(ROOT / "tools/train_eta.py")
            or protocol["numeric_sqlite_reader_sha256"] != sha(ROOT / "tools/fleet_gps.py")):
        raise ValueError("The daily labels use a different timetable or extraction protocol")
    with ZipFile(args.gtfs) as z:
        route_names = {r["route_id"]: r["route_short_name"] for r in csv.DictReader(
            io.TextIOWrapper(z.open("routes.txt"), encoding="utf-8-sig"))}
    models, original = {}, {}
    for region, folder in zip(PROFILES, (args.baseline, args.baseline / "weekend")):
        path = folder / "eta_model.json.gz"
        models[region] = EtaModel(read_data(path))
        if not models[region].last_train_day or models[region].last_train_day >= DAYS[24]:
            raise ValueError("The existing profile includes data on or after the holdout boundary")
        original[region] = {"sha256": sha(path), "train": models[region].metadata["train"]}
    provenance, counts, daily_counts = label_shards(args.labels, args.census, working)
    bank, route_scores = defaultdict(Metrics), {}
    sources = {k: np.zeros(len(HISTORY), dtype=np.int64) for k in ("existing", "candidate")}
    training = defaultdict(Counter)
    parts = defaultdict(list)
    weekend_days = [d.day for d in DAYS if d.weekday() >= 5]
    for shard in range(SHARDS):
        rows = np.fromfile(working / f"{shard}.bin", dtype=LABEL)
        weekends = np.isin(rows["day"], weekend_days)
        per_route = defaultdict(Metrics)
        for region in PROFILES:
            accessible = weekends if region == "weekend" else ~weekends
            for name, flag in zip(FOLD, (1, 2)):
                selected = rows[accessible & ((rows["flags"] & flag) != 0)]
                fitted = fit(selected)
                info = training[name, region]
                info["segments"] += len(selected)
                info["pairs"] += len(fitted["pairs"])
                info["routes_with_history"] += len(set(fitted["routes"]) | {k.split("|", 1)[0] for k in fitted["pairs"]})
                path = working / f"{name}-{region}-{shard}.json"
                write_json(path, fitted)
                parts[name, region].append(path)
                if flag == 1:
                    train_dates = [str(d) for d in DAYS[:24] if (d.weekday() >= 5) == (region == "weekend")]
                    fitted["metadata"] = {"train": train_dates}
                    score(rows[accessible & ((rows["flags"] & 4) != 0)], models[region], EtaModel(fitted),
                          region, bank, sources, per_route, route_names)
        route_scores.update({r: {"name": route_names[r], **m.report()} for r, m in per_route.items()})
    profiles = {}
    for (name, region), paths in parts.items():
        dates = DAYS[:24] if name == FOLD[0] else DAYS
        meta = {"train": [str(d) for d in dates if (d.weekday() >= 5) == (region == "weekend")],
                "day_type": region, "calendar": "upstream GPS civil date in Asia/Kolkata",
                "windows_ist": [["00:00:00", "24:00:00"]], "cutoff_exclusive": str(dates[-1] + timedelta(days=1)),
                **training[name, region]}
        path = output / name / ("weekend" if region == "weekend" else "") / "eta_model.json.gz"
        merge_profile(paths, path, meta)
        profiles[path.relative_to(output).as_posix()] = {**meta, "bytes": path.stat().st_size, "sha256": sha(path)}
    overall = bank["overall", "all"].report()
    if not overall["n"] or overall["n"] != counts["holdout"] or sum(s["n"] for s in route_scores.values()) != counts["holdout"]:
        raise ValueError("The comparison did not account for every held-out segment")
    direction = Counter()
    for details in route_scores.values():
        difference = details["candidate"]["mae_total_seconds"] - details["existing"]["mae_total_seconds"]
        direction["improved" if difference < 0 else "worsened" if difference > 0 else "unchanged"] += 1
    result = {"target": "GPS-observed elapsed time between adjacent stops, in seconds",
              "cutoff": "2026-10-25T00:00:00+05:30", "historical_only": True,
              "fit_rule": "Both stop receipts and the downstream observation's full 90-second lateness window precede the fit cutoff.",
              "overnight_ist": "00:00–04:59", "bias_definition": "forecast minus observed",
              "service_day": "Upstream observation date in Delhi. The archive does not supply an authoritative service date.",
              "gtfs_sha256": gtfs_sha, "label_protocol": protocol, "evaluator_sha256": sha(Path(__file__)),
              "baseline": original, "daily_provenance": provenance,
              "counts": counts, "coverage_by_upstream_day": daily_counts, "profiles": profiles, "overall": overall,
              "by_date": {str(d): bank["day", str(d.day)].report() for d in DAYS[24:]},
              "by_hour_ist": {f"{h:02}": bank["hour", str(h)].report() for h in range(24)},
              "by_group": {g: {k: v.report() for (group, k), v in bank.items() if group == g}
                           for g in ("day_type", "time", "baseline_cohort", "direction")},
              "historical_coverage": {key: dict(zip(HISTORY, map(int, vals))) for key, vals in sources.items()},
              "routes": {"distinct": len(route_scores), **{key: direction[key] for key in ("improved","worsened","unchanged")}},
              "by_route": dict(sorted(route_scores.items(), key=lambda x: int(x[0])))}
    write_json(output / "comparison.json", result)
    write_json(output / "summary.json", compact_summary(result))
    print("Compared", overall["n"], "held-out segments on", len(route_scores), "routes.")
    print(json.dumps(overall, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--labels", required=True, type=Path, help="Directory containing all 31 daily .npz/.json pairs")
    parser.add_argument("--output", required=True, type=Path, help="New directory for result and candidate profiles")
    parser.add_argument("--work", required=True, type=Path, help="Scratch directory with free space for 18 bytes per label")
    parser.add_argument("--gtfs", type=Path, default=ROOT / "delhi_buses_static_gtfs_v1.zip")
    parser.add_argument("--baseline", type=Path, default=ROOT / "data")
    parser.add_argument("--census", type=Path, default=ROOT / "data/october-gps-source-counts.json.gz",
                        help="Locally rebuilt GPS counts file or a directory of the original daily census records")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("--output must be a new directory, separate from the deployed profiles")
    missing = [str(d) for d in DAYS if not all((args.labels / (str(d) + ext)).is_file() for ext in (".npz", ".json"))]
    if missing:
        parser.error("Waiting for all daily labels and completion receipts: " + ", ".join(missing))
    args.work.mkdir(parents=True, exist_ok=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="eta-fit-", dir=args.work) as scratch, TemporaryDirectory(
            prefix="eta-delivery-", dir=args.output.parent) as assembly:
        output = Path(assembly) / "products"
        run(args, Path(scratch), output)
        output.rename(args.output)


if __name__ == "__main__":
    main()
