"""A route-specific platform estimate from fresh stop calls or an upstream bus passage."""
from datetime import datetime, timedelta

from .router import MINIMUM_ALERT_GAIN_SECONDS, _Query


def approaches(router, board):
    """Allow an upstream anchor only when its published onward stop sequence is unique."""
    choices = {}
    for pid, end in board:
        pattern = router.patterns[pid]
        for i in range(max(0, end - 8), end):
            path = tuple(pattern.stops[i:end + 1])
            if len(set(path)) != len(path):
                continue
            variants = choices.setdefault(path[0], {})
            prior = variants.get(path)
            if prior is None or len(pattern.starts) > prior[0]:
                durations = tuple(b - a for a, b in zip(pattern.arrivals[i:end], pattern.arrivals[i+1:end+1]))
                variants[path] = (len(pattern.starts), durations)
    return {stop:next(iter(variants.items())) for stop, variants in choices.items() if len(variants) == 1}


def passage_day(seen, when):
    if not seen.get('service_date'):
        return when.date()
    try:
        day = datetime.strptime(seen['service_date'],'%Y%m%d').date()
    except (TypeError,ValueError):
        return None
    return day if abs((day-when.date()).days)<=1 else None


def upstream_prediction(model, route, paths, when, fresh):
    """Earliest incoming passage within 20 minutes. Each bus uses the same forecast issue clock."""
    issue = int(when.timestamp())
    best = None
    for seen in fresh:
        if seen["route_id"] != route or seen["stop_id"] not in paths:
            continue
        if not issue - 180 <= seen["timestamp"] <= seen["received_at"] <= issue:
            continue
        day = passage_day(seen,when)
        if day is None:
            continue
        midnight = int(datetime.combine(day,datetime.min.time(),when.tzinfo).timestamp())
        path, (_, durations) = paths[seen["stop_id"]]
        at = seen["timestamp"]
        for a, b, scheduled in zip(path, path[1:], durations):
            seconds, _ = model.duration(route, a, b, at - midnight, scheduled,
                                        asof=issue, service_date=day)
            at += seconds
        wait = at - issue
        if 0 <= wait <= 1200 and (best is None or wait < best[0]):
            best = wait, seen
    return best


def platform_departures(query, board):
    return [(ride.departures[i] - query.start, ride.board_source[i], ride) for pid,i in board
            for ride in query.next_rides(pid,i,query.start)]


def boarding_predictions(router, when, board, overlay=None):
    query = _Query(router, when, {"eta":overlay} if overlay is not None else {})
    return [(wait,source) for wait,source,_ in platform_departures(query,board)]


def possible_boarding(query, board, next_departures, wait, route):
    lookup = getattr(query.eta,'live_candidates',None)
    if not lookup or wait is None or wait < MINIMUM_ALERT_GAIN_SECONDS:
        return None
    opportunities = []
    for pid,i in board:
        for day,ride,gap in query.recent_live_departures(lookup,pid,i,query.start):
            identity = ride.trip,ride.live_run_id
            fallback = min((w for w,_,other in next_departures if (other.trip,other.live_run_id)!=identity),default=None)
            if fallback is not None and fallback >= MINIMUM_ALERT_GAIN_SECONDS:
                opportunities.append((gap,fallback,ride.trip,day,ride,i))
    if not opportunities:
        return None
    gap,fallback,trip,day,ride,i = min(opportunities,key=lambda option:option[:4])
    return {"predicted_departure":query.stamp(query.start-gap), "seconds_before_ready":gap,
            "route":route, "trip_id":trip, "fallback_wait_seconds":fallback,
            **query.boarding_evidence(day,ride,i),
            "note":"The live departure estimate has just passed. Board only if the bus is still at this platform and serves your destination."}


def platform_estimate(router, overlay, stop, route, when):
    if stop not in router.stops:
        raise ValueError("Unknown platform ID. Look it up with /stops.")
    if route not in router.routes:
        raise ValueError("Unknown route ID. Use route_id from a bus leg.")
    board = [(pid, i) for pid, i in router.at[stop] if router.patterns[pid].route == route
             and not router.patterns[pid].pickups[i] and i < len(router.patterns[pid].stops) - 1]
    if not board:
        raise ValueError("This route has no boardable service at the selected platform.")
    query = _Query(router, when, {"eta":overlay})
    next_departures = platform_departures(query,board)
    fresh = [p for p in query.position_passages()
             if p["route_id"] == route and overlay.can_approach(p, stop, when)]
    anchor = None
    dated = {}
    for passage in fresh:
        day = passage_day(passage,when)
        if day is not None:
            dated.setdefault(day,[]).append(passage)
    for day,passages in dated.items():
        active = router._active(day)
        active_board = [(pid,i) for pid,i in board if router.patterns[pid].service in active]
        candidate = upstream_prediction(overlay,route,approaches(router,active_board),when,passages)
        if candidate and (anchor is None or candidate[0]<anchor[0]):
            anchor = candidate
    source = wait = observed_at = observed_stop = None
    eligible = sorted((w,s) for w,s,_ in next_departures)
    # Prefer a matched stop call when it is the earliest catchable departure in
    # the route search. A later live bus must not obscure an earlier incoming estimate.
    if eligible and eligible[0][1] == "live" and (anchor is None or eligible[0][0] <= anchor[0]):
        wait, source = eligible[0]
        note = "Matched live stop prediction for this platform."
    elif anchor:
        wait, seen = anchor
        source = "upstream_gps"
        observed_at = datetime.fromtimestamp(seen["timestamp"], when.tzinfo).isoformat()
        observed_stop = router.stops[seen["stop_id"]]
        note = "A bus on this route was seen upstream. Confirm its destination before boarding."
    elif eligible:
        wait, source = eligible[0]
        note = "Estimated using the route timetable and available running times."
    else:
        note = "No approaching service was found within the search limits."
    route_name = router.routes[route][0]
    possible = possible_boarding(query, board, next_departures, wait, route_name)
    return {"platform":router.stops[stop], "route_id":route, "route":route_name,
            "at":when.isoformat(), "arrival":(when + timedelta(seconds=wait)).isoformat() if wait is not None else None,
            "wait_seconds":wait, "source":source, "observed_at":observed_at, "observed_stop":observed_stop, "note":note,
            **({"possible_boarding":possible} if possible else {})}
