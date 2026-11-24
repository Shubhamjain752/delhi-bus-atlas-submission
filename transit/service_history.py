"""Historical service observations for one directed platform pair, by Delhi civil clock."""
from datetime import date
import gzip
import json
from pathlib import Path

from .schedule import portable_cache_name


class ServiceHistory:
    def __init__(self, raw, named):
        self.raw = raw
        self.named = named

    def coverage(self):
        raw = self.raw or {}
        named = self.named or {}
        return {"training_start":raw.get("training_start"),"training_end":raw.get("training_end"),
                "origin_windows":len(raw.get("buckets",{})), "platform_gap_windows":len(raw.get("platform_gaps",{})),
                "matched_start_windows":len(named.get("buckets",{}))}

    @classmethod
    def load(cls, directory, feed, index):
        def matching(filename):
            path = Path(directory) / filename
            if not path.is_file():
                return None
            with gzip.open(path, "rt", encoding="utf-8") as source:
                raw = json.load(source)
            name = index.name if index.name.startswith("router-v2-sha256-") else portable_cache_name(feed)
            if raw.get("version") == 1 and name == f"router-v2-sha256-{raw.get('feed_sha256')}.sqlite":
                return raw
            return None
        return cls(matching("origin-service-history.json.gz"), matching("from-2026-11/scheduled-start-check.json.gz"))

    def _cell(self, raw, field, route, origin, following, when, asof):
        if raw is None or min(when.date(), asof.date()) < date.fromisoformat(raw["available_from"]):
            return None, None
        day_type = "weekend" if when.weekday() >= 5 else "weekday"
        band = when.hour // 2
        key = f"{route}|{origin['id']}|{following['id']}|{day_type}|{band}"
        return raw.get(field, {}).get(key), {"day_type":day_type,"start_hour":2*band,"next_stop":following}

    def origin(self, route, origin, following, when, asof):
        cell, base = self._cell(self.raw, "buckets", route, origin, following, when, asof)
        if cell is None:
            return None
        result = {**base,"training_start":self.raw["training_start"],"training_end":self.raw["training_end"],
                  "origin":origin, **{k:cell[k] for k in
                  ("scheduled","observed","observed_days","gap_count","gap_days")}}
        if cell["gap_count"] >= 5 and cell["gap_days"] >= 3 and cell["median_gap_seconds"] is not None:
            result["median_gap_seconds"] = cell["median_gap_seconds"]
        return result

    def platform(self, route, platform, following, when, asof):
        cell, base = self._cell(self.raw, "platform_gaps", route, platform, following, when, asof)
        if cell is None:
            return None
        count, days, median, p90 = cell
        if count < 5 or days < 3:
            return None
        return {**base,"training_start":self.raw["training_start"],"training_end":self.raw["training_end"],
                "platform":platform,"gap_count":count,"gap_days":days,
                "median_gap_seconds":median,"p90_gap_seconds":p90}

    def scheduled(self, route, origin, following, when, asof):
        cell, base = self._cell(self.named, "buckets", route, origin, following, when, asof)
        if not cell or not 0 <= cell["matched"] <= cell["scheduled"] or not cell["scheduled"] or not cell["observed_dates"]:
            return None
        return {**base,"origin":origin, "matched":cell["matched"], "scheduled":cell["scheduled"],
                "observed_dates":cell["observed_dates"]}
