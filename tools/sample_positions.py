"""Collect a short paired VehiclePositions/TripUpdates series for dated validation."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import gzip
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from transit.live import download, PUBLIC_TRIPS


def collect(directory, polls, interval):
    directory.mkdir(parents=True, exist_ok=True)
    feeds = {"positions":PUBLIC_TRIPS.replace("trip_updates", "vehicle_positions"), "predictions":PUBLIC_TRIPS}
    with ThreadPoolExecutor(max_workers=2) as pool:
        for index in range(polls):
            start = time.monotonic()
            futures = {tag:pool.submit(download, url) for tag,url in feeds.items()}
            for tag, future in futures.items():
                try:
                    feed = future.result()
                except (OSError, ValueError) as error:
                    print(tag, type(error).__name__, flush=True)
                    continue
                receipt = int(time.time())
                target = directory / f"{tag}-{receipt}-{feed.header.timestamp}.pb.gz"
                with gzip.open(target, "wb") as out:
                    out.write(feed.SerializeToString())
                print(index, tag, receipt - feed.header.timestamp, len(feed.entity), flush=True)
            if index + 1 < polls:
                time.sleep(max(0, interval - (time.monotonic() - start)))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--polls", type=int, default=25)
    parser.add_argument("--interval", type=int, default=30)
    args = parser.parse_args()
    if args.polls < 1 or args.interval < 15:
        parser.error("Choose at least one poll and at least 15 seconds between polls.")
    collect(args.directory, args.polls, args.interval)
