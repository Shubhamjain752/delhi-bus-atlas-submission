"""Rebuild October source counts locally without retaining individual bus records."""
import argparse
from functools import lru_cache
import gzip
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DEFAULT_COUNTS = ROOT / "data/october-gps-source-counts.json.gz"
DAYS = tuple(f"2026-10-{d:02}" for d in range(1, 32))


@lru_cache(maxsize=2)
def load_counts(source):
    with gzip.open(source, "rt", encoding="utf-8") as stream:
        data = json.load(stream)
    if data["schema"] != 1 or set(data["days"]) != set(DAYS):
        raise ValueError("Expected the October 2026 route and hour totals for all 31 dates")
    return data["days"]


def read_census(day, source=DEFAULT_COUNTS):
    return load_counts(Path(source))[str(day)]


def pack_counts(source_rows, target):
    days = {}
    for day, record in source_rows:
        census = {key: record["census"][key] for key in ("raw_records", "by_hour", "by_route")}
        total = census["raw_records"]
        if not (record["complete"] and record["day"] == day and total == sum(census["by_route"].values())
                and total == sum(census["by_hour"].values())):
            raise ValueError(f"The source counts for {day} do not reconcile")
        days[day] = {"day": day, "complete": True, "archive_member": {"crc32": record["archive_member"]["crc32"]},
                     "census": census}
    if set(days) != set(DAYS):
        raise ValueError("An October reference needs all 31 dates")
    encoded = json.dumps({"schema": 1, "days": days}, sort_keys=True, separators=(",", ":")).encode("utf-8")
    pending = target.with_name(target.name + ".building")
    pending.write_bytes(gzip.compress(encoded, compresslevel=9, mtime=0))
    pending.replace(target)
    return sum(item["census"]["raw_records"] for item in days.values())


def existing_studies(directory):
    for day in DAYS:
        with gzip.open(directory / f"{day}.json.gz", "rt", encoding="utf-8") as stream:
            yield day, json.load(stream)


def archive_studies(archive, scratch, seven):
    from tools.month_boarding_scan import PREVIOUS_EXTRACTS, archive_members, daily_database
    from tools.month_census import scan_census

    scratch.mkdir(parents=True, exist_ok=True)
    members = archive_members(seven, archive)
    for day in DAYS:
        member = members[day]
        with daily_database(seven, archive, member, PREVIOUS_EXTRACTS, scratch) as (database, _):
            census = scan_census(database)
        print(f"Checked the source for {day}", flush=True)
        yield day, {"day": day, "complete": True, "archive_member": member, "census": census}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--source-days", type=Path, help="Existing directory of 31 completed daily boarding studies")
    source.add_argument("--archive", type=Path, help="Count your original October GPS archive")
    parser.add_argument("--scratch", type=Path, help="Directory for one temporary daily database")
    parser.add_argument("--seven", type=Path, default=Path("C:/Program Files/7-Zip/7z.exe"))
    parser.add_argument("--output", type=Path, default=DEFAULT_COUNTS)
    args = parser.parse_args()
    if args.archive and not args.scratch:
        parser.error("--scratch is required to count the original archive")
    records = archive_studies(args.archive, args.scratch, args.seven) if args.archive else existing_studies(args.source_days)
    total = pack_counts(records, args.output)
    print(f"Wrote route and hour counts for {total:,} broadcasts across 31 dates")
