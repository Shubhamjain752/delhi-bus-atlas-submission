"""Stop-to-stop ETA estimates and strictly as-of historical GPS replay."""

from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
import gzip
import json
from pathlib import Path
from statistics import median


IST = timezone(timedelta(hours=5, minutes=30))
BIN_SECONDS = 2 * 3600
LOOKBACK = 30 * 60


def epoch(asof):
    if asof is None:
        return None
    if isinstance(asof, datetime):
        if asof.tzinfo is None:
            asof = asof.replace(tzinfo=IST)
        return int(asof.timestamp())
    return int(asof)


def read_data(path):
    with gzip.open(path, "rt", encoding="utf-8") as src:
        return json.load(src)


class EtaModel:
    """Learned medians with sparse-cell shrinkage and a dated replay index."""

    def __init__(self, fitted=None, replay=None):
        fitted = fitted or {}
        self.pairs = fitted.get("pairs", {})
        self.route_ratios = fitted.get("routes", {})
        self.metadata = fitted.get("metadata", {})
        self.weekend = None
        self.earlier = None
        dates = self.metadata.get("train", [])
        self.last_train_day = date.fromisoformat(max(dates)) if dates else None
        cutoff = self.metadata.get("cutoff_exclusive")
        self.available_from = (date.fromisoformat(cutoff) if cutoff else
                               self.last_train_day + timedelta(days=1) if self.last_train_day else None)
        self.trained_through = (int(datetime.combine(self.available_from, datetime.min.time(), IST).timestamp())
                                if self.available_from else None)
        self.passages = sorted((replay or {}).get("passages", []))
        self.passage_clock = [p[0] for p in self.passages]
        self.recent = defaultdict(list)
        self.recent_clock = {}
        for received, observed, route, origin, dest, duration in (replay or {}).get("segments", []):
            self.recent[(route, origin, dest)].append((received, observed, duration))
        for key, values in self.recent.items():
            values.sort()
            self.recent_clock[key] = [v[0] for v in values]

    @classmethod
    def load(cls, data_dir: Path, *, include_forward=False):
        """Load the named research fit. The journey planner also loads its dated successor."""
        directory = Path(data_dir)
        monthly = directory / "from-2026-11"
        use_monthly = include_forward and monthly.is_dir()

        def load_folder(folder):
            model_file = folder / "eta_model.json.gz"
            replay_file = folder / "eta_replay.json.gz"
            original = read_data(model_file) if model_file.is_file() else None
            replay = read_data(replay_file) if replay_file.is_file() else None
            if use_monthly:
                model = cls(read_data(monthly / folder.relative_to(directory) / "eta_model.json.gz"), replay)
                model.earlier = cls(original)
                return model
            return cls(original, replay)

        model = load_folder(directory)
        if use_monthly or (directory / "weekend" / "eta_model.json.gz").is_file():
            model.weekend = load_folder(directory / "weekend")
            model.metadata = {**model.metadata,
                              "day_types":{"weekday":model.metadata,"weekend":model.weekend.metadata}}
        return model

    def duration(self, route_id: str, from_stop: str, to_stop: str, service_second: int,
                 scheduled_seconds: int, asof=None, service_date=None) -> tuple[int, str]:
        """Pick a prior by Delhi service date and use only observations available as of the query."""
        now = epoch(asof)
        target = service_date
        if isinstance(target, datetime):
            target = target.astimezone(IST).date() if target.tzinfo else target.date()
        elif isinstance(target,str):
            target = date.fromisoformat(target)
        asof_day = None
        if now is not None and (self.weekend is not None or target is not None):
            asof_day = datetime.fromtimestamp(now,IST).date()
        day = target or asof_day
        profile = self.weekend if self.weekend is not None and day and day.weekday() >= 5 else self
        history = profile
        while history.available_from and (
                target is not None and target < history.available_from or
                now is not None and now < history.trained_through):
            history = history.earlier
            if history is None:
                return max(1,int(scheduled_seconds)), "timetable"
        # A retrospective/advance query for a different day can use its prior.
        # Receipts from another service date must not appear as live evidence.
        recent_clock = None if target and asof_day != target else now
        return profile._estimate(route_id,from_stop,to_stop,service_second,scheduled_seconds,recent_clock,history)

    def _estimate(self,route_id,from_stop,to_stop,service_second,scheduled_seconds,now,history):
        base = max(1,int(scheduled_seconds))
        route = str(route_id)
        start, end = str(from_stop), str(to_stop)
        time_bin = str((int(service_second) % 86400) // BIN_SECONDS)
        ratio = history.route_ratios.get(route)
        pair = history.pairs.get("|".join((route, start, end)))
        estimate = float(base)
        source = "timetable"
        if ratio:
            overall, bins = ratio
            estimate *= bins.get(time_bin, overall)
            source = "gps_historical"
        if pair:
            overall, bins = pair
            count, duration = overall
            estimate = (count * duration + 3 * estimate) / (count + 3)
            if time_bin in bins:
                count, duration = bins[time_bin]
                estimate = (count * duration + 5 * estimate) / (count + 5)
            source = "gps_historical"
        key = (route, start, end)
        if now is not None and key in self.recent_clock:
            clock = self.recent_clock[key]
            lo = bisect_left(clock, now - LOOKBACK)
            hi = bisect_left(clock, now)
            sample = [v[2] for v in self.recent[key][lo:hi] if now - LOOKBACK <= v[1] < now]
            if sample:
                weight = len(sample) / (len(sample) + 5)
                estimate = (1 - weight) * estimate + weight * median(sample)
                source = "gps_historical_recent"
        return max(1, round(estimate)), source

    def recent_passages(self, asof, max_age=180):
        """Latest passage per vehicle-run known at asof. A passage is a GPS proxy."""
        now = epoch(asof)
        if now is None:
            return []
        if self.weekend is not None and datetime.fromtimestamp(now,IST).weekday() >= 5:
            return self.weekend.recent_passages(now,max_age)
        lo = bisect_left(self.passage_clock, now - max_age)
        hi = bisect_right(self.passage_clock, now)
        runs = {}
        for received, observed, route, run, stop in self.passages[lo:hi]:
            if not now - max_age <= observed <= now:
                continue
            if run not in runs or observed > runs[run]["timestamp"]:
                runs[run] = {"route_id": route, "trip_id": run, "stop_id": stop,
                             "timestamp": observed, "received_at": received}
        return list(runs.values())
