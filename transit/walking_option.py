"""An optional direct walk, kept separate from all bus itineraries."""

from datetime import datetime, timedelta

from .walking import SPEED


MAX_DIRECT_WALK_M = 2000


def walking_alternative(walking, result):
    """Return a verified graph path if it beats every returned bus, or no bus was found."""
    origin = result["origin"]
    destination = result["destination"]
    start = (origin["lat"], origin["lon"])
    end = (destination["lat"], destination["lon"])
    if walking is None or start == end:
        return None

    path = walking.route(start, end, max_distance_m=MAX_DIRECT_WALK_M)
    if not path or not 0 < path["distance_m"] <= MAX_DIRECT_WALK_M or path["seconds"] <= 0:
        return None

    bus_seconds = min(
        (journey["duration_seconds"] for journey in result["journeys"]
         if any(leg["mode"] == "bus" for leg in journey["legs"])),
        default=None,
    )
    saved = None if bus_seconds is None else bus_seconds - path["seconds"]
    if saved is not None and saved <= 0:
        return None

    departure = datetime.fromisoformat(result["departure"])
    return {
        **path,
        "mode": "walk",
        "from": origin,
        "to": destination,
        "departure": departure.isoformat(),
        "arrival": (departure + timedelta(seconds=path["seconds"])).isoformat(),
        "assumed_pace_m_s": SPEED,
        "time_saved_seconds": saved,
        "reason": "no_bus_within_limits" if saved is None else "faster_than_bus",
    }
