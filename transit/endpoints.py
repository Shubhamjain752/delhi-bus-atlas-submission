"""Match stop names, preserve explicit endpoints and recover useful trips to nearby stops."""
from collections import Counter
from itertools import product

from .router import approximate_m, coords, normalized


SAME_NAME_RADIUS_M = 250
MAX_ALTERNATE_PLATFORMS = 2
NEARBY_ALIGHTING_RADIUS_M = 400
MAX_NEARBY_EXIT_SEARCHES = 3


def suggestions(router, query, limit, *, include_entrances=False):
    matches = router.search(query, limit)
    for stop in matches:
        onward = Counter()
        for pattern_id, index in router.at.get(stop["id"], ()):
            pattern = router.patterns[pattern_id]
            if index + 1 < len(pattern.stops) and pattern.pickups[index] == 0:
                name = router.stops[pattern.stops[index + 1]]["name"]
                if name != stop["name"]:
                    onward[name] += 1
        if onward:
            stop["via_stop"] = onward.most_common(1)[0][0]
    if include_entrances:
        name = normalized(query)
        gate_ids = dict.fromkeys(gate for alias, gate in router.entrance_names.items() if name and name in alias)
        if gate_ids:
            exact_count = sum(stop["id"] == query or normalized(stop["name"]) == name for stop in matches)
            # Keep a station query's first platform. A fully typed gate can take first place.
            insert_at = exact_count or (0 if name in router.entrance_names else min(1, len(matches)))
            matches[insert_at:insert_at] = [router.entrances[gate].copy() for gate in gate_ids]
    return matches[:limit]


def nearby_names(router, text, chosen):
    candidates = [(0, chosen)]
    if not isinstance(text, str) or chosen["id"] is None or text in router.stops:
        return candidates
    for identifier in router.exact[router.names[chosen["id"]]]:
        if identifier != chosen["id"]:
            other = router.stops[identifier]
            distance = approximate_m(coords(chosen), coords(other))
            if distance <= SAME_NAME_RADIUS_M:
                candidates.append((distance, other))
    # On a tie keep the original first, including for different IDs at the same coordinates.
    return sorted(candidates, key=lambda entry: entry[0])[:1 + MAX_ALTERNATE_PLATFORMS]


def plan_with_stop_names(router, origin, destination, depart, **options):
    result = router.journeys(origin, destination, depart, **options)
    starts = nearby_names(router, origin, result["origin"])
    ends = nearby_names(router, destination, result["destination"])
    if not result["journeys"] and (len(starts) > 1 or len(ends) > 1):
        pairs = sorted(product(starts, ends), key=lambda pair: pair[0][0] + pair[1][0])
        for (_, start), (_, end) in pairs[1:]:
            alternate = router.journeys(start, end, depart, **options)
            if alternate["journeys"]:
                result = alternate
                break
    for field, matches in (("origin", starts), ("destination", ends)):
        if len(matches) > 1:
            stop = result[field]
            result["warnings"].append(
                f"Several platforms share the name {stop['name']}. The {field} used here is stop {stop['id']}. "
                "Choose a specific stop from the list to use a different platform."
            )
    return result


def nearby_alighting_option(router, result, depart, **options):
    """Keep the selected destination fixed when only part of the trip can be routed."""
    if result["journeys"]:
        return None
    destination = result["destination"]
    end = coords(destination)
    tried = 0
    best = None
    for _, stop_id in router._nearby(end, NEARBY_ALIGHTING_RADIUS_M):
        if stop_id == destination["id"]:
            continue
        if not any(index > 0 and router.patterns[pid].drops[index] == 0 for pid, index in router.at[stop_id]):
            continue
        stop = router.stops[stop_id]
        if router.walking and router.walking.route(coords(stop), end, max_distance_m=result["limits"]["max_walk_m"]):
            continue
        attempt = router.journeys(result["origin"], stop, depart, **options)
        tried += 1
        for journey in attempt["journeys"]:
            buses = [i for i, leg in enumerate(journey["legs"]) if leg["mode"] == "bus"]
            if not buses:
                continue
            last = buses[-1]
            leg = journey["legs"][last]
            alighting = leg["to"]
            metres = approximate_m(coords(alighting), end)
            if alighting["id"] == destination["id"] or metres > NEARBY_ALIGHTING_RADIUS_M:
                continue
            if (alighting["id"] != stop_id and router.walking and
                    router.walking.route(coords(alighting), end, max_distance_m=result["limits"]["max_walk_m"])):
                continue
            # The trial was routed to a nearby platform. If it included a walk there,
            # end this option at the actual bus exit instead of inheriting that unrelated walk.
            egress = journey["egress_walk_seconds"]
            to_bus_exit = {**journey, "arrival": leg["arrival"], "legs": journey["legs"][:last + 1],
                           "duration_seconds": journey["duration_seconds"] - egress,
                           "walk_seconds": journey["walk_seconds"] - egress, "egress_walk_seconds": 0}
            option = {"journey": to_bus_exit, "alighting_stop": alighting, "destination": destination,
                      "straight_line_distance_m": round(metres)}
            road_walk = getattr(router.walking, "estimated_route", None)
            estimate = road_walk(coords(alighting), end, max_distance_m=result["limits"]["max_walk_m"]) if road_walk else None
            if estimate:
                option["estimated_walk"] = estimate
            rank = (estimate is None, to_bus_exit["duration_seconds"] + estimate["seconds"] if estimate else metres,
                    to_bus_exit["duration_seconds"])
            if best is None or rank < best[0]:
                best = (rank, option)
        if tried >= MAX_NEARBY_EXIT_SEARCHES:
            break
    return best[1] if best else None
