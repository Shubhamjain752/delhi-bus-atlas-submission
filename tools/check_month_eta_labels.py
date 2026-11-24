"""Compare a bounded month-extractor fixture with the original ETA label builder."""

import argparse
from collections import Counter
from datetime import datetime
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.train_eta import clock, extract, gtfs_routes
from transit.eta import read_data


COLUMNS = ("route", "from_stop", "to_stop", "start", "end", "received", "seconds", "scheduled")


def make_reference(args):
    day = datetime.strptime(args.database.stem, "bus_movements_%Y_%m_%d").date().isoformat()
    routes = gtfs_routes(args.gtfs, set(read_data(args.route_profile)["routes"]))
    start, end = clock(args.start), clock(args.end)
    if not 0 <= start < end <= 86400:
        raise ValueError("Choose one increasing receipt-time interval inside the database day")
    rows, _, pings = extract(args.database, day, routes, [(start, end)])
    # The original duration is the difference between the two observed stop-entry clocks.
    labels = [(int(route), int(a), int(b), at - duration, at, received, duration, scheduled)
              for received, at, route, a, b, duration, scheduled, _, _ in rows]
    if not labels:
        raise ValueError("A useful parity fixture must contain observed stop pairs")
    data = np.asarray(labels, dtype=np.int64)
    np.savez_compressed(args.output, **dict(zip(COLUMNS, data.T)))
    print(f"Reference: {len(routes)} routes considered, {pings:,} received positions, {len(labels):,} labels")


def read_counter(path):
    with np.load(path, allow_pickle=False) as archive:
        values = [archive[name] for name in COLUMNS]
        if len({len(value) for value in values}) != 1:
            raise ValueError(f"Unequal column lengths in {path}")
        return Counter(zip(*(value.tolist() for value in values), strict=True))


def compare(reference, candidate):
    expected = read_counter(reference)
    actual = read_counter(candidate)
    missing = sum((expected - actual).values())
    extra = sum((actual - expected).values())
    print(f"Reference {expected.total():,}, candidate {actual.total():,}, missing {missing:,}, extra {extra:,}")
    return int(bool(missing or extra))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    reference = commands.add_parser("reference")
    reference.add_argument("database", type=Path)
    reference.add_argument("output", type=Path)
    reference.add_argument("--gtfs", type=Path, default=ROOT / "delhi_buses_static_gtfs_v1.zip")
    reference.add_argument("--route-profile", type=Path, default=ROOT / "data/eta_model.json.gz")
    reference.add_argument("--start", default="08:30:00")
    reference.add_argument("--end", default="08:35:00")
    check = commands.add_parser("compare")
    check.add_argument("reference", type=Path)
    check.add_argument("candidate", type=Path)
    args = parser.parse_args()
    if args.command == "reference":
        make_reference(args)
    else:
        sys.exit(compare(args.reference, args.candidate))
