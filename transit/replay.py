"""Replay public feeds in receipt order without changing the clock of the live service."""
from datetime import datetime
import gzip
from pathlib import Path

from .eta import IST
from .live import LiveOverlay, TTL, protobuf


class CapturedOverlay(LiveOverlay):
    def __init__(self, model, directory, issue):
        super().__init__(model, directory)
        self.issue = issue

    def eligible(self, stamp, asof):
        # Only the requested historical instant can consume this snapshot.
        return asof == self.issue and stamp > 0 and 0 <= asof - stamp <= TTL


def received_files(directory):
    entries = []
    for path in Path(directory).glob("*.pb.gz"):
        parts = path.name.split("-")
        if len(parts) == 3 and parts[0] in {"positions","predictions"} and parts[1].isdigit():
            entries.append((int(parts[1]), parts[0], path))
    return sorted(entries)


def restore_entries(overlay, entries, issue):
    used = []
    for receipt, kind, path in entries:
        if receipt > issue:
            break
        with gzip.open(path, "rb") as source:
            feed = protobuf().FeedMessage.FromString(source.read())
        if kind == "positions":
            overlay.apply_positions(feed, receipt, provider="community_positions")
        else:
            overlay.apply_trip_updates(feed, receipt)
        used.append(path.name)
    return used


def load_november_capture(router, model, directory, issue):
    entries = received_files(directory / "nov21-samples")
    if not entries:
        raise FileNotFoundError("The November feed capture is missing.")
    # The first comparison starts at the beginning of the first captured minute.
    first, last = entries[0][0], entries[-1][0]
    if not first - first % 60 <= issue <= last:
        raise ValueError("The archived November feed covers 21 November 2026, 01:17 to 01:30 IST.")
    overlay = CapturedOverlay(model, directory, issue).bind(router)
    used = restore_entries(overlay, entries, issue)
    cutoff = max((receipt for receipt, _, _ in entries if receipt <= issue), default=None)
    metadata = {"id":"nov21", "received_files":len(used),
                "last_receipt":datetime.fromtimestamp(cutoff, IST).isoformat() if cutoff is not None else None}
    return overlay, metadata
