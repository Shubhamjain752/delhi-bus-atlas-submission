"""Count every archived broadcast by reported route and Delhi receipt hour."""
from collections import Counter
from pathlib import Path
import sqlite3
from contextlib import closing


def scan_census(path: Path):
    """Use a single read-only scan; the SQL implementation accepts nonstandard archives."""
    try:
        from tools.month_census_native import scanner
        native = scanner()
    except (ImportError, OSError):
        native = None
    if native is not None:
        result = native(path)
        if result is not None:
            return result
    return sql_census(path)


def sql_census(path: Path):
    """Read bounded rowid ranges, including records with missing route or clock."""
    uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
    by_hour = Counter()
    by_route = Counter()
    first_clock = last_clock = None
    with closing(sqlite3.connect(uri, uri=True)) as db:
        db.execute("PRAGMA temp_store=MEMORY")
        low, high = db.execute("SELECT min(rowid), max(rowid) FROM vehicle_feed").fetchone()
        if low is not None:
            step = 750_000
            for start in range(low, high + 1, step):
                rows = db.execute("""
                    SELECT route_id, substr(time, 1, 2), count(*), min(time), max(time)
                    FROM vehicle_feed WHERE rowid >= ? AND rowid < ?
                    GROUP BY route_id, substr(time, 1, 2)
                """, (start, start + step))
                for route, hour, count, earliest, latest in rows:
                    if isinstance(route, float) and route.is_integer():
                        route = int(route)
                    by_route[str(route) if route is not None else "missing"] += count
                    by_hour[hour if hour in VALID_HOURS else "unknown"] += count
                    if earliest is not None and (first_clock is None or earliest < first_clock):
                        first_clock = earliest
                    if latest is not None and (last_clock is None or latest > last_clock):
                        last_clock = latest
    return {"raw_records": sum(by_route.values()), "by_hour": dict(sorted(by_hour.items())),
            "by_route": dict(sorted(by_route.items())), "first_receipt_clock": first_clock,
            "last_receipt_clock": last_clock}


VALID_HOURS = {f"{hour:02d}" for hour in range(24)}
