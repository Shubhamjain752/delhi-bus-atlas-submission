"""Earliest-arrival search on fixed bus patterns and a routable pedestrian graph."""

import json
import math
import re
import sqlite3
import zipfile
from bisect import bisect_left
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from functools import cached_property, lru_cache
from inspect import signature

from .schedule import cache_schedule, rows
from .service_history import ServiceHistory
from .boarding import PlatformPath


IST = timezone(timedelta(hours=5, minutes=30))
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
ALL_STOPS_SERVED = frozenset()
BOARDING_UNCERTAINTY_SECONDS = 60
MINIMUM_ALERT_GAIN_SECONDS = 300
TRANSFER_ENDPOINT_LIMIT = 12
NEARBY_PLATFORM_SCREEN_M = 250
NEARBY_PLATFORM_COUNT = 2
DEPARTURE_BOARD_SECONDS = 30 * 60
POSITION_BOARD_STOPS = 8
POSITION_BOARD_SECONDS = 20 * 60


def normalized(text):
    return " ".join(re.findall(r"[\w]+", str(text).casefold()))


def approximate_m(a, b):
    dy = (a[0] - b[0]) * 111195
    dx = (a[1] - b[1]) * 111195 * math.cos(math.radians((a[0] + b[0]) / 2))
    return math.hypot(dx, dy)


def coords(stop):
    return stop["lat"], stop["lon"]


def local(dt):
    return dt.astimezone(IST) if dt.tzinfo else dt.replace(tzinfo=IST)


@dataclass
class Pattern:
    route: str
    service: str
    shape: str
    stops: list
    arrivals: list
    departures: list
    pickups: list
    drops: list
    sequences: list
    starts: list
    trips: list


@dataclass
class Ride:
    pid: int
    trip: str
    arrivals: list
    departures: list
    sources: list
    board_source: list
    skipped: frozenset
    service_date: date
    live_run_id: str | None = None
    forecast_calls: frozenset | None = None
    position_anchor: tuple | None = None

    def serves(self, index):
        return index not in self.skipped and (self.forecast_calls is None or index in self.forecast_calls)


class Router:
    def __init__(self, gtfs_path, data_dir, walking, eta):
        self.gtfs_path = gtfs_path
        self.cache_path = cache_schedule(gtfs_path, data_dir)
        self.service_history = ServiceHistory.load(data_dir, gtfs_path, self.cache_path)
        self.walking, self.eta = walking, eta
        self.patterns, self.at = [], defaultdict(list)
        with closing(sqlite3.connect(self.cache_path)) as db:
            meta = {k: json.loads(v) for k,v in db.execute("SELECT * FROM metadata")}
            self.stops = {i: {"id": i, "name": n, "lat": lat, "lon": lon} for i,n,lat,lon in meta["stops"]}
            self.routes = {i: (n,a) for i,n,a in meta["routes"]}
            for route, service, shape, *fields in db.execute("SELECT * FROM patterns"):
                stops, arrivals, departures, pickups, drops, sequences, trips = map(json.loads, fields)
                if not all(s in self.stops for s in stops):
                    continue
                starts, ids = zip(*trips)
                p = Pattern(route,service,shape,stops,arrivals,departures,pickups,drops,sequences,starts,ids)
                for k, s in enumerate(stops):
                    self.at[s].append((len(self.patterns), k))
                self.patterns.append(p)
        self.calendar = meta["calendar"]
        self.exceptions = {(r["date"],r["service_id"]): r["exception_type"] for r in meta["exceptions"]}
        self.feed_info = meta["feed"]
        self.stats = {"stops": len(self.stops), "routes": len(self.routes), "patterns": len(self.patterns),
                      "trips": sum(len(p.trips) for p in self.patterns), "feed_info": self.feed_info}
        self.names, self.exact, self.grid = {}, defaultdict(list), defaultdict(list)
        for s, val in self.stops.items():
            self.names[s] = normalized(val["name"])
            self.exact[self.names[s]].append(s)
            self.grid[self._cell(coords(val))].append(s)
        self.max_service = max((p.starts[-1]+p.departures[-1] for p in self.patterns),default=0)

    @staticmethod
    def _cell(c):
        return (math.floor(c[0]*150), math.floor(c[1]*150))

    def _nearby(self, c, radius):
        y,x = self._cell(c)
        cells = max(1, math.ceil(radius / 500))
        result = []
        for dy in range(-cells,cells+1):
            for dx in range(-cells,cells+1):
                for s in self.grid.get((y+dy,x+dx), ()):
                    d = approximate_m(c, coords(self.stops[s]))
                    if d <= radius and s in self.at:
                        result.append((d,s))
        return sorted(result)

    def search(self, query, limit=10):
        q = normalized(query)
        if not q:
            return []
        tokens = q.split()
        candidates = [s for s,n in self.names.items() if s == str(query) or q in n
                      or all(token in n for token in tokens)]
        candidates.sort(key=lambda s:(s != str(query), self.names[s] != q, not self.names[s].startswith(q),
                                      len(self.names[s]), -len(self.at.get(s, ())), s))
        return [self.stops[s].copy() for s in candidates[:limit]]

    def _place(self, value):
        if isinstance(value, dict):
            if str(value.get("id")) in self.stops:
                return self.stops[str(value["id"])]
            value = (value["lat"], value["lon"])
        if isinstance(value, str) and value in self.stops:
            return self.stops[value]
        point = value if isinstance(value,(list,tuple)) else str(value).split(",")
        if len(point) == 2:
            try:
                lat,lon = map(float,point)
            except (TypeError,ValueError):
                pass
            else:
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    raise ValueError("Coordinates are outside the valid latitude/longitude range.")
                return {"id": None, "name": f"{lat:.5f}, {lon:.5f}", "lat":lat,"lon":lon}
        result = self.search(str(value),1)
        if not result:
            raise ValueError(f"Could not find a GTFS stop named '{value}'. Use a stop name, ID or latitude,longitude.")
        return result[0]

    @lru_cache(maxsize=60)
    def _active(self, day):
        d = day.strftime("%Y%m%d")
        active = {r["service_id"] for r in self.calendar
                  if r["start_date"] <= d <= r["end_date"] and r[WEEKDAYS[day.weekday()]] == "1"}
        for (date, service), operation in self.exceptions.items():
            if date == d:
                if operation == "1":
                    active.add(service)
                else:
                    active.discard(service)
        return active

    @lru_cache(maxsize=500)
    def _geometry(self, pid):
        p = self.patterns[pid]
        with closing(sqlite3.connect(self.cache_path)) as db:
            row = db.execute("SELECT points FROM shapes WHERE id=?", (p.shape,)).fetchone()
        fallback = [list(coords(self.stops[s])) for s in p.stops]
        if not row:
            return fallback, list(range(len(fallback))), "ordered_stops"
        points = json.loads(row[0])
        if len(points) < 2:
            return fallback, list(range(len(fallback))), "ordered_stops"
        indexes, start = [], 0
        for lat,lon in fallback:
            pos = min(range(start,len(points)),key=lambda k:
                      (points[k][0]-lat)**2 + 0.77*(points[k][1]-lon)**2)
            if approximate_m((lat,lon),points[pos]) > 350:
                return fallback, list(range(len(fallback))), "ordered_stops"
            indexes.append(pos)
            start = pos
        return points, indexes, "gtfs_shape"

    @lru_cache(maxsize=500)
    def shape_measures(self, pid):
        """Metres along the published shape at stops with a close alignment."""
        points, indexes, kind = self._geometry(pid)
        if kind != "gtfs_shape":
            return [None] * len(indexes)
        cumulative = [0.0]
        for first, second in zip(points, points[1:]):
            cumulative.append(cumulative[-1] + approximate_m(first, second))
        return [cumulative[k] if approximate_m(coords(self.stops[s]), points[k]) <= 75 else None
                for s, k in zip(self.patterns[pid].stops, indexes)]

    @lru_cache(maxsize=2048)
    def platform_path(self, pid, index):
        pattern = self.patterns[pid]
        if index+1 >= len(pattern.stops) or not pattern.shape:
            return None
        points, indexes, kind = self._geometry(pid)
        if kind != 'gtfs_shape' or approximate_m(points[indexes[index+1]],coords(self.stops[pattern.stops[index+1]])) > 75:
            return None
        return PlatformPath.build(points,indexes,index,coords(self.stops[pattern.stops[index]]))

    @cached_property
    def trip_directions(self):
        # Older portable indexes omit this optional GTFS column.
        with zipfile.ZipFile(self.gtfs_path) as feed:
            return {row["trip_id"]:int(row["direction_id"]) for row in rows(feed,"trips.txt")
                    if row.get("direction_id") in ("0","1")}

    def journeys(self, origin, destination, depart, **kwargs):
        return _Query(self, local(depart), kwargs).run(self._place(origin), self._place(destination))


class _Query:
    def __init__(self, router, depart, options):
        self.r, self.depart = router, depart
        self.asof = local(options.get("asof") or depart)
        self.midnight = depart.replace(hour=0,minute=0,second=0,microsecond=0)
        self.start = int((depart-self.midnight).total_seconds())
        self.horizon = self.start + int(options.get("horizon_seconds", 14400))
        self.limit = max(1, min(8,int(options.get("limit",3))))
        self.transfers = max(0,min(1,int(options.get("max_transfers",1))))
        self.buffer = max(0,int(options.get("transfer_buffer_seconds",90)))
        self.max_walk = max(0,int(options.get("max_walk_m",1100)))
        self.transfer_walk = max(0,int(options.get("transfer_walk_m",220)))
        self.walk_cache, self.timelines, self.itineraries = {}, {}, {}
        self.platform_services = {}
        self.earlier_boardings = {}
        self.eta = options.get("eta", router.eta)
        exclusions=getattr(self.eta,"trip_exclusions",None)
        self.exclusions=exclusions(int(self.asof.timestamp())) if exclusions else {}
        lookup = getattr(self.eta,"vehicle_runs",None)
        self.instances = lookup(int(self.asof.timestamp())) if lookup else {}
        self._position_passages = None
        position_lookup = getattr(self.eta,"journey_positions",None)
        self.position_runs = (position_lookup(self.asof) if position_lookup
                              and options.get("use_position_board_times",True) else {})
        self.instance_boards = defaultdict(set)
        for (day,trip), runs in self.instances.items():
            self.exclusions.pop((day,trip),None)  # All restrictions are now applied per confirmed vehicle.
            pid,k = self.eta.ids[trip]
            for run in runs:
                for stop in run.times:
                    self.instance_boards[day,pid,stop].add(k)
        self.bounded_walk = bool(router.walking and "max_distance_m" in signature(router.walking.route).parameters)
        self.days = []
        forecast_days = set(getattr(self.eta,'forecast_service_dates',lambda asof: ())(int(self.asof.timestamp())))
        forecast_days.update(day for day,_ in self.position_runs)
        for offset in (-1,0,1):
            day = (self.midnight+timedelta(days=offset)).date()
            if offset == -1 and self.r.max_service < self.start + 86400 and day not in forecast_days:
                continue
            if offset == 1 and self.horizon < 86400 and day not in forecast_days:
                continue
            self.days.append((86400*offset, day, self.r._active(day)))

    def walk(self, a, b):
        ca, cb = coords(a), coords(b)
        if ca == cb:
            if a["id"] and b["id"] and a["id"] != b["id"]:
                # Different boarding platforms with only a shared coordinate have no verified crossing.
                return None
            return {"seconds":0,"distance_m":0,"geometry":[list(ca)],"source":"same_stop"}
        if (ca,cb) not in self.walk_cache:
            options = {"max_distance_m": max(self.max_walk,self.transfer_walk)} if self.bounded_walk else {}
            self.walk_cache[ca,cb] = self.r.walking.route(ca,cb,**options) if self.r.walking else None
        return self.walk_cache[ca,cb]

    def endpoint(self, place, outgoing):
        # An explicit platform can always be used, even if it has no mapped footpath.
        nearby = self.r._nearby(coords(place), self.max_walk)
        if place["id"] in self.r.at:
            nearby = [(0,place["id"])] + [e for e in nearby if e[1] != place["id"]]
        result = {}
        for _,s in nearby:
            stop = self.r.stops[s]
            w = self.walk(place,stop) if outgoing else self.walk(stop,place)
            if w and w["distance_m"] <= self.max_walk:
                result[s] = w
                if len(result) >= TRANSFER_ENDPOINT_LIMIT:
                    break
        return result

    def timeline(self, pid, k, day, offset, instance=None, boarding_index=None, position=None):
        key = pid,k,offset,instance.live_run_id if instance else None,boarding_index,position
        if key in self.timelines:
            return self.timelines[key]
        p = self.r.patterns[pid]
        start = p.starts[k]
        if instance is None and (day,p.trips[k]) in self.instances:
            return None
        skipped=instance.skipped if instance else self.exclusions.get((day,p.trips[k]),ALL_STOPS_SERVED)
        if skipped is None:
            return None
        arrivals, departures, sources, board_sources = [], [], [], []
        origin_epoch = int(self.midnight.timestamp())+offset
        live_fn = getattr(self.eta,"departure",None)
        boarding_fn = getattr(self.eta,"boarding_departure",live_fn)
        now = int(self.asof.timestamp())
        forecast_calls = set() if instance else None
        last_live = None
        for i,stop in enumerate(p.stops):
            sch_arr,sch_dep = start+p.arrivals[i],start+p.departures[i]
            source = "schedule"
            if i:
                run = sch_arr-start-p.arrivals[i-1]
                if self.eta:
                    run, source = self.eta.duration(p.route,p.stops[i-1],stop,
                        arrivals[-1]-offset,run,asof=self.asof,service_date=day)
                # GPS stop passages bracket upstream dwell and road travel together.
                arrival = max(departures[-1],arrivals[-1]+max(0,round(run)))
            else:
                arrival = offset+sch_arr
            if position is not None and i == position[0]:
                arrival = position[1] - int(self.midnight.timestamp())
            departure = arrival if i in skipped else arrival+sch_dep-sch_arr
            if instance:
                live = instance.times.get(stop)
                if live is not None:
                    if live <= now + 5400 and (i == boarding_index or now-30 <= live):
                        forecast_calls.add(i)
                    else:
                        live = None
            else:
                lookup = boarding_fn if i == boarding_index else live_fn
                live = lookup(p.trips[k],stop,day,origin_epoch+sch_dep,now) if lookup else None
            if live is not None:
                # A vehicle can be far ahead of its timetable. Keep the live
                # clock fixed and reconcile only the inferred times preceding it.
                live_depart = int(live) - int(self.midnight.timestamp())
                live_arrive = live_depart - (sch_dep - sch_arr)
                if last_live is None:
                    shift = live_arrive - arrival
                    for n in range(i):
                        arrivals[n] += shift
                        departures[n] += shift
                        if shift:
                            board_sources[n] = "live_inferred"
                else:
                    fixed = departures[last_live]
                    live_depart = max(fixed, live_depart)
                    live_arrive = max(fixed, live_arrive)
                    if arrival > fixed:
                        scale = (live_arrive - fixed) / (arrival - fixed)
                        for n in range(last_live + 1, i):
                            arrivals[n] = fixed + round((arrivals[n] - fixed) * scale)
                            departures[n] = fixed + round((departures[n] - fixed) * scale)
                            board_sources[n] = "live_inferred"
                            sources[n] = "live_inferred"
                arrival, departure = live_arrive, live_depart
                last_live = i
                source = "live"
            arrivals.append(arrival)
            departures.append(departure)
            sources.append(source)
            board_sources.append("live" if live is not None else
                                 "upstream_gps" if position is not None and i > position[0] else
                                 "schedule" if not i else source)
        result = Ride(pid,p.trips[k],arrivals,departures,sources,board_sources,skipped,day,
                      instance.live_run_id if instance else None, frozenset(forecast_calls) if instance else None,
                      position)
        self.timelines[key] = result
        return result

    def trip_timelines(self, pid, k, day, offset, boarding_index=None):
        trip = self.r.patterns[pid].trips[k]
        for instance in self.instances.get((day,trip), (None,)):
            ride = self.timeline(pid,k,day,offset,instance,boarding_index)
            if ride is not None:
                yield ride

    def next_rides(self, pid, i, ready, *, before_walk_ready=None):
        p = self.r.patterns[pid]
        choices = []
        for offset,day,active in self.days:
            if p.service not in active:
                continue
            j = bisect_left(p.starts,ready-offset-p.departures[i])
            # A delayed prior departure or an early nominal departure can change a connection.
            prev = max(0,j-2,bisect_left(p.starts,ready-offset-p.departures[i]-3600))
            indexes = set()
            eligible = 0
            for k in range(prev,len(p.starts)):
                if p.starts[k]+offset+p.departures[i] > self.horizon+1800:
                    break
                skipped=self.exclusions.get((day,p.trips[k]),ALL_STOPS_SERVED)
                if skipped is None or i in skipped:
                    continue
                indexes.add(k)
                if k>=j and not skipped and (day,p.trips[k]) not in self.instances:
                    eligible+=1
                    if eligible==4:
                        break
            live_options=getattr(self.eta,"live_candidates",None)
            recent_calls = ()
            if live_options:
                midnight=int(self.midnight.timestamp())
                asof = int(self.asof.timestamp())
                recent_calls = live_options(pid,p.stops[i],day,0,midnight+ready-1,asof)
                indexes.update(live_options(pid,p.stops[i],day,midnight+ready,midnight+self.horizon,
                                            asof))
            indexes.update(self.instance_boards.get((day,pid,p.stops[i]), ()))
            position_runs = self.position_runs.get((day,pid),{})
            indexes.update(position_runs)
            for k in sorted(indexes):
                position = position_runs.get(k)
                if position is not None and i != position[0]:
                    # A fix at the boarding platform cannot by itself establish
                    # when the doors close. Retain the original forecast there.
                    if i < position[0]:
                        continue
                    if i-position[0] <= POSITION_BOARD_STOPS:
                        ride = self.timeline(pid,k,day,offset,position=position)
                        if ride is None:
                            continue
                        issue = int((self.asof-self.midnight).total_seconds())
                        if ride.departures[i] <= issue + POSITION_BOARD_SECONDS:
                            if ride.serves(i) and max(ready,issue) <= ride.departures[i] <= self.horizon:
                                choices.append(ride)
                            continue
                # A recent expired call still takes precedence over its nominal timetable departure.
                board_index = i if k in recent_calls else None
                for ride in self.trip_timelines(pid,k,day,offset,boarding_index=board_index):
                    if ride.serves(i) and ready <= ride.departures[i] <= self.horizon:
                        choices.append(ride)
        if before_walk_ready is not None:
            choices = [ride for ride in choices if ride.departures[i] < before_walk_ready
                       and not self.observed_downstream(ride.service_date,ride,i)]
        ordered=sorted(choices,key=lambda ride:ride.departures[i])
        if any(ride.live_run_id for ride in ordered):
            # One vehicle may overtake another, or only forecast part of the route.
            best = {}
            for ride in ordered:
                for j in range(i+1,len(p.stops)):
                    if not p.drops[j] and ride.serves(j) and (j not in best or ride.arrivals[j] < best[j].arrivals[j]):
                        best[j] = ride
            return [ride for ride in ordered if any(ride is winner for winner in best.values())
                    or ride is ordered[0]]
        if not ordered or not ordered[0].skipped:
            return ordered[:1]
        # A later departure can be the first bus that serves a skipped alighting point.
        selected=[ordered[0]]
        missing={j for j in ordered[0].skipped if j>i and not p.drops[j]}
        for ride in ordered[1:]:
            if not missing:
                break
            if missing-ride.skipped:
                selected.append(ride)
                missing.intersection_update(ride.skipped)
        return selected

    def save(self, access, rides, transfer, egress):
        arrival = rides[-1][0].arrivals[rides[-1][2]]+egress["seconds"]
        if arrival > self.horizon:
            return
        signature = tuple(self.r.patterns[ride.pid].route for ride,_,_ in rides)
        previous = self.itineraries.get(signature)
        walking = access["seconds"]+egress["seconds"]+(transfer["seconds"] if transfer else 0)
        cost = arrival,walking
        if previous is None or cost < previous[0]:
            self.itineraries[signature] = cost,(access,rides,transfer,egress)

    def remaining_direct_links(self, access, egress):
        """Check direct stop pairs before pruning away either end for transfer search."""
        ends = defaultdict(list)
        for _, stop in self.r._nearby(coords(self.destination), self.max_walk):
            for pid, j in self.r.at[stop]:
                if not self.r.patterns[pid].drops[j]:
                    ends[pid].append((j, stop))

        unverified = []
        for distance, stop in self.r._nearby(coords(self.origin), self.max_walk):
            for pid, i in self.r.at[stop]:
                pattern = self.r.patterns[pid]
                if pattern.pickups[i]:
                    continue
                missing = [(j, end) for j, end in ends.get(pid, ())
                           if j > i and not (stop in access and end in egress)]
                if not missing:
                    continue
                approach = self.walk(self.origin, self.r.stops[stop])
                verified = approach is not None and approach["distance_m"] <= self.max_walk
                if not verified and (distance > NEARBY_PLATFORM_SCREEN_M or self.origin["id"] is not None):
                    continue
                exits = []
                for j, end in missing:
                    walk = self.walk(self.r.stops[end], self.destination)
                    if walk is not None and walk["distance_m"] <= self.max_walk:
                        exits.append((j, walk))
                if not exits:
                    continue
                if not verified:
                    unverified.append((distance, pid, i, exits))
                    continue
                ready = self.start + approach["seconds"]
                self.collect_earlier_boardings(pid, i, approach, exits)
                for ride in self.next_rides(pid, i, ready):
                    for j, walk in exits:
                        if ride.serves(j):
                            self.save(approach, [(ride, i, j)], None, walk)
        return unverified

    def collect_earlier_boardings(self, pid, i, approach, exits):
        """A departure visible from the platform that the mapped access walk misses."""
        walk_ready = self.start + approach["seconds"]
        earliest = max(self.start + 1, math.ceil((self.asof-self.midnight).total_seconds()))
        if earliest >= walk_ready or not exits:
            return
        pattern = self.r.patterns[pid]
        board = pattern.stops[i]
        for ride in self.next_rides(pid, i, earliest, before_walk_ready=walk_ready):
            departure = ride.departures[i]
            for j, walk in exits:
                finish = ride.arrivals[j] + walk["seconds"]
                if not ride.serves(j) or ride.arrivals[j] <= departure or finish > self.horizon:
                    continue
                # At each boarding pole show the next direct departure for each
                # line. Choose the best verified exit for that departure.
                key = board, self.r.routes[pattern.route][0]
                rank = departure, finish, walk["seconds"], pattern.route, ride.trip, j
                if key in self.earlier_boardings and self.earlier_boardings[key][0] <= rank:
                    continue
                self.earlier_boardings[key] = rank, {
                    **self.service_times(ride,i,j),
                    "boarding_stop":self.r.stops[board], "alighting_stop":self.r.stops[pattern.stops[j]],
                    "service_date":ride.service_date.isoformat(),
                    "access_walk_seconds":approach["seconds"], "access_walk_distance_m":approach["distance_m"],
                    "walk_ready_at":self.stamp(walk_ready), "seconds_before_walk_ready":walk_ready-departure,
                    "egress_walk_seconds":walk["seconds"],
                }

    def nearby_platform_options(self, unverified):
        """Display a stop departure separately when the approach on foot is unknown."""
        boards = defaultdict(dict)
        distances = {}
        for distance, pid, i, exits in unverified:
            pattern = self.r.patterns[pid]
            if (pattern.route,) in self.itineraries:
                continue
            # Geometric distance only screens out departures that are already too soon
            # at the usual walking pace. It supplies no passenger path or arrival time.
            earliest = self.start + max(1, math.ceil(distance / 1.25))
            for ride in self.next_rides(pid, i, earliest):
                if ride.departures[i] > self.start + DEPARTURE_BOARD_SECONDS:
                    continue
                for j, walk in exits:
                    if not ride.serves(j) or ride.arrivals[j] <= ride.departures[i]:
                        continue
                    finish = ride.arrivals[j] + walk["seconds"]
                    if finish > self.horizon:
                        continue
                    platform = pattern.stops[i]
                    distances[platform] = distance
                    board = boards[platform]
                    label = self.r.routes[pattern.route][0]
                    rank = (ride.departures[i], finish, pattern.route)
                    if label in board and board[label][0] <= rank:
                        continue
                    board[label] = rank, {
                        **self.service_times(ride, i, j), "boarding_stop":self.r.stops[platform],
                        "alighting_stop":self.r.stops[pattern.stops[j]],
                        "straight_line_to_board_m":round(distance),
                        "egress_walk_seconds":walk["seconds"],
                    }
        best = {}
        for platform in sorted(boards, key=distances.get)[:NEARBY_PLATFORM_COUNT]:
            for label, candidate in boards[platform].items():
                best.setdefault(label, candidate)
        return [option for _,option in sorted(best.values(), key=lambda item:item[0])]

    def service_times(self, ride, i, j):
        route = self.r.patterns[ride.pid].route
        return {"route_id":route, "route":self.r.routes[route][0], "trip_id":ride.trip,
                "departure_at":self.stamp(ride.departures[i]), "alight_at":self.stamp(ride.arrivals[j]),
                "departure_source":ride.board_source[i], **self.position_evidence(ride)}

    def position_evidence(self, ride):
        if ride.position_anchor is None:
            return {}
        index, observed, received = ride.position_anchor
        return {"boarding_position": {"observed_stop_id":self.r.patterns[ride.pid].stops[index],
                                      "observed_at":datetime.fromtimestamp(observed,IST).isoformat(),
                                      "received_at":datetime.fromtimestamp(received,IST).isoformat()}}

    def service_evidence(self, ride, board):
        p = self.r.patterns[ride.pid]
        start = next(i for i, restriction in enumerate(p.pickups) if restriction == 0)
        origin = self.r.stops[p.stops[start]]
        midnight = datetime.combine(ride.service_date, datetime.min.time(), IST)
        start_time = midnight + timedelta(seconds=p.starts[p.trips.index(ride.trip)] + p.departures[start])
        departure = {"origin":origin, "scheduled_at":start_time.isoformat(), "service_date":ride.service_date.isoformat(),
                     "status":"unconfirmed"}
        anchor = ride.position_anchor
        # A matched stop fix can establish that a bus is in service even when it cannot
        # time boarding (for example, a bus standing at the passenger's platform).
        if anchor is None and not ride.live_run_id:
            lookup = getattr(self.eta, "journey_positions", None)
            if lookup:
                anchors = lookup(self.asof).get((ride.service_date,ride.pid), {})
                anchor = anchors.get(p.trips.index(ride.trip))
        if anchor is None:
            observed_position = getattr(self.eta, "matched_service_position", None)
            if observed_position:
                anchor = observed_position(ride, self.asof)
        if anchor is not None:
            index, observed, received = anchor
            issue = int(self.asof.timestamp())
            if (index > start and issue - 180 <= observed <= received <= issue and
                    approximate_m(coords(origin), coords(self.r.stops[p.stops[index]])) > 90):
                departure.update(status="seen_in_service", observed_stop=self.r.stops[p.stops[index]],
                                 observed_at=datetime.fromtimestamp(observed,IST).isoformat(),
                                 received_at=datetime.fromtimestamp(received,IST).isoformat())
        result = {"origin_departure":departure}
        history = self.r.service_history
        if start+1 < len(p.stops):
            following = self.r.stops[p.stops[start+1]]
            origin_stats = history.origin(p.route, origin, following, start_time, self.asof)
            if origin_stats:
                result["service_history"] = origin_stats
            named = history.scheduled(p.route, origin, following, start_time, self.asof)
            if named:
                result["scheduled_start_history"] = named
        if board+1 not in ride.skipped:
            board_time = self.midnight + timedelta(seconds=ride.departures[board])
            platform_gap = history.platform(p.route, self.r.stops[p.stops[board]], self.r.stops[p.stops[board+1]],
                                            board_time, self.asof)
            if platform_gap:
                result["platform_gap_history"] = platform_gap
        return result

    def same_platform_services(self, item):
        """Other lines a passenger can board here and leave at the same exit stop."""
        approach, [(chosen, first, last)], _, egress = item
        pattern = self.r.patterns[chosen.pid]
        board, exit_stop = pattern.stops[first], pattern.stops[last]
        ready = self.start + approach["seconds"]
        key = board, exit_stop, ready
        if key not in self.platform_services:
            exits = defaultdict(list)
            for pid, j in self.r.at[exit_stop]:
                if not self.r.patterns[pid].drops[j]:
                    exits[pid].append(j)
            by_line = {}
            for pid, i in self.r.at[board]:
                candidate = self.r.patterns[pid]
                downstream = [j for j in exits.get(pid, ()) if j > i]
                if candidate.pickups[i] or not downstream:
                    continue
                for ride in self.next_rides(pid, i, ready):
                    if ride.departures[i] > ready + DEPARTURE_BOARD_SECONDS:
                        continue
                    for j in downstream:
                        if not ride.serves(j) or ride.arrivals[j] <= ride.departures[i]:
                            continue
                        if ride.arrivals[j] + egress["seconds"] > self.horizon:
                            continue
                        rank = ride.departures[i], ride.arrivals[j], candidate.route
                        label = self.r.routes[candidate.route][0]
                        if label not in by_line or rank < by_line[label][0]:
                            by_line[label] = rank, self.service_times(ride, i, j)
            self.platform_services[key] = [s for _,s in sorted(by_line.values(), key=lambda x:x[0])]
        selected_line = self.r.routes[pattern.route][0]
        return [service for service in self.platform_services[key] if service["route"] != selected_line]

    @staticmethod
    def distinct_choices(ranked):
        """Keep a change onto a through bus only if it reduces the passenger's walk."""
        def final_call(rides):
            ride, _, j = rides[-1]
            # Trip IDs can recur on another day or on another live vehicle.
            return ride.pid, ride.trip, ride.live_run_id, j, ride.arrivals[j]
        through = {final_call(rides): cost for cost, (_, rides, _, _) in ranked if len(rides) == 1}
        distinct = []
        for cost, item in ranked:
            rides = item[1]
            direct_cost = through.get(final_call(rides))
            if len(rides) == 1 or direct_cost is None or direct_cost > cost:
                distinct.append((cost, item))
        return distinct

    def run(self, origin, destination):
        self.origin, self.destination = origin,destination
        if coords(origin)==coords(destination) and (None in (origin["id"],destination["id"])
                                                     or origin["id"]==destination["id"]):
            dt=self.depart.isoformat()
            same={"mode":"walk","from":origin,"to":destination,"departure":dt,"arrival":dt,"seconds":0,
                  "geometry":[list(coords(origin))],"source":"same_location","distance_m":0}
            return self.result([{"departure":dt,"arrival":dt,"duration_seconds":0,"transfers":0,
                                 "walk_seconds":0,"access_walk_seconds":0,"transfer_walk_seconds":0,
                                 "egress_walk_seconds":0,"wait_seconds":0,"ride_seconds":0,"legs":[same]}],[])
        access, egress = self.endpoint(origin,True), self.endpoint(destination,False)
        destinations = defaultdict(list)
        for s in egress:
            for pid,j in self.r.at[s]:
                if self.r.patterns[pid].drops[j] == 0:
                    destinations[pid].append((j,egress[s]))
        first = defaultdict(list)
        for s,w in access.items():
            for pid,i in self.r.at[s]:
                p = self.r.patterns[pid]
                if p.pickups[i] or i == len(p.stops)-1 or (not self.transfers and pid not in destinations):
                    continue
                self.collect_earlier_boardings(pid, i, w, [(j,walk) for j,walk in destinations.get(pid,()) if j>i])
                for ride in self.next_rides(pid,i,self.start+w["seconds"]):
                    for j,walk in destinations.get(pid,()):
                        if j>i and ride.serves(j):
                            self.save(w,[(ride,i,j)],None,walk)
                    if self.transfers:
                        for j in range(i+1,len(p.stops)):
                            if not p.drops[j] and ride.serves(j) and ride.arrivals[j] < self.horizon:
                                first[p.stops[j]].append((ride.arrivals[j],pid,w,ride,i,j))
        if self.transfers:
            self.connect(first,destinations)
        unverified = self.remaining_direct_links(access, egress)
        ranked = sorted(self.itineraries.values(),key=lambda v:v[0])
        distinct = self.distinct_choices(ranked)
        selected = distinct[:self.limit]
        if self.limit > 1 and selected and all(len(item[1][1]) > 1 for item in selected):
            direct = next((item for item in distinct if len(item[1][1]) == 1),None)
            if direct:
                selected[-1] = direct
        plans = [self.render_feasible(item) for _,item in selected]
        warnings = []
        if not plans and (not access or not egress):
            warnings.append("No mapped walking connection to a serviced stop within the endpoint walking limit.")
        elif not plans:
            warnings.append("No catchable itinerary found within the search time, walking and transfer limits.")
        if any(re.search(r"SPL|SPECIAL",leg.get("route",""),re.I) for p in plans for leg in p["legs"]):
            warnings.append("Special service appears in these results. Confirm eligibility with the operator.")
        alerts = self.boarding_alerts(access, destinations, ranked)
        platform_options = self.nearby_platform_options(unverified) if not plans or plans[0]["transfers"] else []
        return self.result(plans,warnings,alerts,platform_options)

    def recent_live_departures(self, live_candidates, pid, i, ready):
        """Yield direct observations at this boarding platform just before the rider is ready."""
        p = self.r.patterns[pid]
        midnight = int(self.midnight.timestamp())
        asof = int(self.asof.timestamp())
        for offset, day, active in self.days:
            if p.service not in active:
                continue
            indices = live_candidates(pid, p.stops[i], day, midnight + ready - BOARDING_UNCERTAINTY_SECONDS,
                                      midnight + ready - 1, asof)
            for k in set(indices).union(self.instance_boards.get((day,pid,p.stops[i]), ())):
                for ride in self.trip_timelines(pid,k,day,offset,boarding_index=i):
                    if not ride.serves(i) or ride.board_source[i] != "live":
                        continue
                    gap = ready - ride.departures[i]
                    if 0 < gap <= BOARDING_UNCERTAINTY_SECONDS and not self.observed_downstream(day,ride,i):
                        yield day, ride, gap

    def position_passages(self):
        if self._position_passages is None:
            lookup = getattr(self.eta, 'recent_passages', None)
            self._position_passages = lookup(self.asof) if lookup else ()
        return self._position_passages

    def forecast_identity(self, day, ride):
        seen_by = int(self.asof.timestamp())
        if ride.live_run_id:
            runs = self.instances.get((day,ride.trip), ())
            return next(((r.vehicle_id,r.provider_trip) for r in runs if r.live_run_id==ride.live_run_id),None)
        lookup = getattr(self.eta,'forecast_vehicle',None)
        return lookup(day,ride.trip,seen_by) if lookup else None

    def boarding_evidence(self, day, ride, board):
        identity = self.forecast_identity(day,ride)
        lookup = getattr(self.eta,'boarding_position',None)
        age,approaching = lookup(day,ride.pid,board,identity,int(self.asof.timestamp())) if identity and lookup else (None,False)
        return {'service_date':day.isoformat(),'gps_age_seconds':age,'vehicle_approaching':approaching}

    def observed_downstream(self, day, ride, board):
        """A known later passage by this dated run rules out waiting for it at an earlier stop."""
        pattern = self.r.patterns[ride.pid]
        seen_by = int(self.asof.timestamp())
        suffix = pattern.stops[board+1:]
        identity = self.forecast_identity(day,ride)
        if not identity:
            return False
        passed = getattr(self.eta,'passed_platform',None)
        if passed and passed(day,ride.pid,board,identity,seen_by):
            return True
        for row in self.position_passages():
            if (row.get('service_date') == day.strftime('%Y%m%d') and row['route_id'] == pattern.route and
                    (row.get('position_vehicle'),row.get('position_trip')) == identity and
                    seen_by-180 <= row['timestamp'] <= row['received_at'] <= seen_by and
                    row['stop_id'] in suffix and pattern.stops.count(row['stop_id']) == 1):
                return True
        return False

    def boarding_alerts(self, access, destinations, ranked):
        live_candidates = getattr(self.eta, "live_candidates", None)
        if not live_candidates or not ranked:
            return []
        fastest_arrival = ranked[0][0][0]
        candidates = {}
        for stop, walk in access.items():
            ready = self.start + walk["seconds"]
            for pid, i in self.r.at[stop]:
                p = self.r.patterns[pid]
                ends = [(j,w) for j,w in destinations.get(pid, ()) if j > i]
                if p.pickups[i] or not ends:
                    continue
                for day, ride, gap in self.recent_live_departures(live_candidates, pid, i, ready):
                    for j, egress in ends:
                        if not ride.serves(j) or ride.arrivals[j] <= ride.departures[i]:
                            continue
                        # Compare to a bus hypothetically remaining at the platform until the rider was ready.
                        arrival = ride.arrivals[j] + gap + egress["seconds"]
                        if fastest_arrival - arrival < MINIMUM_ALERT_GAIN_SECONDS:
                            continue
                        rank = arrival, walk["seconds"], gap
                        key = day, ride.trip, ride.live_run_id
                        if key in candidates and candidates[key][0] <= rank:
                            continue
                        label = self.r.routes[p.route][0]
                        platform = self.r.stops[stop]
                        unit = "second" if gap == 1 else "seconds"
                        alert = {
                            "route":label, "route_id":p.route, "trip_id":ride.trip,
                            **({"live_run_id":ride.live_run_id} if ride.live_run_id else {}),
                            "platform":platform, "alighting_stop":self.r.stops[p.stops[j]],
                            "predicted_departure":self.stamp(ride.departures[i]), "ready_at":self.stamp(ready),
                            "seconds_before_ready":gap, "access_walk_seconds":walk["seconds"],
                            **self.boarding_evidence(day, ride, i),
                            "source":"live", "catchable":False,
                            "message":f"At {platform['name']}, the live departure estimate for {label} is "
                                      f"{gap} {unit} before your platform arrival. The bus may already have left."
                        }
                        candidates[key] = rank, alert, (walk,ride,i,j,egress)
        alerts = []
        for _,alert,boarding in sorted(candidates.values(),key=lambda item:item[0])[:self.limit]:
            self.add_conditional_itinerary(alert, boarding, ranked)
            alerts.append(alert)
        return alerts

    def add_conditional_itinerary(self, alert, boarding, ranked):
        access, ride, i, j, egress = boarding
        ready = self.start + access["seconds"]
        fallback = None
        for _, item in ranked:
            first, index, _ = item[1][0]
            platform_id = self.r.patterns[first.pid].stops[index]
            if platform_id == alert["platform"]["id"] and first.departures[index] > ready:
                fallback = item
                break
        if fallback is None:
            return
        # This counterfactual is private to the option. Strict search and the original forecasts retain their clocks.
        gap = alert["seconds_before_ready"]
        adjusted = replace(ride, arrivals=[t + gap for t in ride.arrivals], departures=[t + gap for t in ride.departures])
        option = self.render((access, [(adjusted,i,j)], None, egress))
        option.update({"boarding_status":"only_if_bus_is_present", "shift_seconds":gap,
                       "assumption":"If the bus is still boarding when you reach the platform, board immediately. "
                                    "The estimate keeps the predicted ride duration and shifts downstream stop times by the departure difference."})
        alert["conditional_itinerary"] = option
        alert["fallback_itinerary"] = self.render_feasible(fallback)

    def result(self, plans, warnings, alerts=None, platform_options=None):
        return {"origin":self.origin,"destination":self.destination,"departure":self.depart.isoformat(),
                "journeys":plans,"boarding_alerts":alerts or [], "direct_from_nearby_platforms":platform_options or [],
                "earlier_boarding_options":[option for _, option in
                    sorted(self.earlier_boardings.values(),key=lambda item:item[0])],
                "warnings":warnings,"limits":{"max_transfers":self.transfers,
                "horizon_seconds":self.horizon-self.start,"max_walk_m":self.max_walk,
                "transfer_walk_m":self.transfer_walk,"transfer_buffer_seconds":self.buffer,
                "endpoint_stops_considered":TRANSFER_ENDPOINT_LIMIT,"direct_endpoints":"all_in_walking_radius",
                "max_transfer_candidates":180},
                "source":"GTFS fixed-route schedule, walking graph and available ETA estimates"}

    def connect(self, first, destinations):
        downstream = defaultdict(list)
        for pid, ends in destinations.items():
            p = self.r.patterns[pid]
            for i,s in enumerate(p.stops):
                if p.pickups[i]:
                    continue
                for j,egress in ends:
                    if i < j:
                        downstream[s].append((pid,i,j,egress))
        candidates = {}
        for s,labels in first.items():
            labels.sort(key=lambda l:l[0])
            # Keep the earliest arriving first bus for each of up to two route choices.
            unique = {}
            for label in labels:
                unique.setdefault(self.r.patterns[label[1]].route,label)
                if len(unique)==2:
                    break
            near = [(0,s)]
            if self.transfer_walk:
                near += [(d,t) for d,t in self.r._nearby(coords(self.r.stops[s]),self.transfer_walk)
                         if t!=s and t in downstream][:4]
            for d,t in near:
                for pid,i,j,endwalk in downstream.get(t,()):
                    p = self.r.patterns[pid]
                    for rt,label in unique.items():
                        if rt == p.route:
                            continue
                        # Geometric distance ranks candidates only; the walking graph verifies each transfer.
                        rank = label[0]+int(d/1.25)+self.buffer+p.arrivals[j]-p.departures[i]+endwalk["seconds"]
                        item = (rank,label,s,t,pid,i,j,endwalk)
                        key = rt,pid,i,t
                        if key not in candidates or rank<candidates[key][0]:
                            candidates[key]=item
        counts, evaluated = Counter(), 0
        for _,label,s,t,pid,i,j,endwalk in sorted(candidates.values(),key=lambda c:c[0]):
            pair = self.r.patterns[label[1]].route,self.r.patterns[pid].route
            if counts[pair] >= 5:
                continue
            transfer = self.walk(self.r.stops[s],self.r.stops[t])
            if not transfer or transfer["distance_m"]>self.transfer_walk:
                continue
            counts[pair]+=1
            evaluated += 1
            ready = label[0]+transfer["seconds"]+self.buffer
            for ride2 in self.next_rides(pid,i,ready):
                if not ride2.serves(j):
                    continue
                _,_,access,ride1,board,alight = label
                self.save(access,[(ride1,board,alight),(ride2,i,j)],transfer,endwalk)
            if evaluated>=180:
                break

    def stamp(self, seconds):
        return (self.midnight+timedelta(seconds=int(seconds))).isoformat()

    def stop_calls(self, ride, board, alight):
        p = self.r.patterns[ride.pid]
        measures = self.r.shape_measures(ride.pid)
        calls = []
        previous = None
        for k in range(board, alight + 1):
            if k in ride.skipped:
                continue
            seconds = distance = speed = None
            if previous is not None:
                seconds = ride.arrivals[k] - ride.departures[previous]
                if measures[k] is not None and measures[previous] is not None:
                    metres = measures[k] - measures[previous]
                    if metres > 0:
                        distance = round(metres)
                        # Suppress unstable speeds on collapsed/short shape links or unrealistic clock jumps.
                        if metres >= 50 and seconds >= 5 and 3.6 * metres / seconds <= 100:
                            speed = round(3.6 * metres / seconds, 1)
            calls.append({"stop_id":p.stops[k], "arrival":self.stamp(ride.arrivals[k]),
                          "departure":self.stamp(ride.departures[k]), "source":ride.board_source[k],
                          "preceding_segment_seconds":seconds, "path_distance_m":distance,
                          "estimated_speed_kmh":speed})
            previous = k
        return calls

    def render_feasible(self, item):
        plan = self.render(item)
        if plan["transfers"] == 0:
            services = self.same_platform_services(item)
            if services:
                bus = next(leg for leg in plan["legs"] if leg["mode"] == "bus")
                bus["same_platform_services"] = services
        return plan

    def render(self, item):
        access,rides,transfer,egress = item
        legs = []
        cursor = self.start

        def leg(mode,a,b,end,geo,source,**extras):
            nonlocal cursor
            legs.append({"mode":mode,"from":a,"to":b,"departure":self.stamp(cursor),"arrival":self.stamp(end),
                         "seconds":int(end-cursor),"geometry":geo,"source":source,**extras})
            cursor = end

        def walk_leg(a,b,walk):
            detail={k:walk[k] for k in ("distance_m","includes_arterial_without_mapped_sidewalk",
                                         "origin_snap_m","destination_snap_m","uses_steps",
                                         "inferred_crossing_access_m") if k in walk}
            leg("walk",a,b,cursor+walk["seconds"],walk["geometry"],walk["source"],**detail)

        for n,(ride,i,j) in enumerate(rides):
            p = self.r.patterns[ride.pid]
            a, b = self.r.stops[p.stops[i]], self.r.stops[p.stops[j]]
            walk = transfer if n else access
            previous = legs[-1]["to"] if n else self.origin
            if walk["seconds"] or previous["id"]!=a["id"]:
                walk_leg(previous,a,walk)
            if ride.departures[i] > cursor:
                leg("wait",a,a,ride.departures[i],[list(coords(a))],ride.board_source[i],
                    transfer_buffer_seconds=self.buffer if n else 0)
            points,index,geo_source = self.r._geometry(ride.pid)
            geometry = points[index[i]:index[j]+1]
            if len(geometry) < 2:
                geometry = [list(coords(a)),list(coords(b))]
                geo_source = "ordered_stops"
            sources = dict(Counter(ride.sources[i+1:j+1]))
            name,agency = self.r.routes.get(p.route,(p.route,""))
            leg("bus",a,b,ride.arrivals[j],geometry,", ".join(sorted(sources)),route_id=p.route,route=name,
                agency=agency,trip_id=ride.trip,geometry_source=geo_source,model_sources=sources,
                **({"live_run_id":ride.live_run_id} if ride.live_run_id else {}),
                **self.position_evidence(ride),
                **self.service_evidence(ride, i),
                stop_count=sum(k not in ride.skipped for k in range(i+1,j+1)),
                calls=self.stop_calls(ride,i,j),
                stops=[self.r.stops[p.stops[k]] for k in range(i,j+1) if k not in ride.skipped],
                skipped_stops=[self.r.stops[p.stops[k]] for k in range(i,j+1) if k in ride.skipped])
        end = legs[-1]["to"]
        if egress["seconds"] or end["id"]!=self.destination["id"]:
            walk_leg(end,self.destination,egress)
        totals = Counter()
        for l in legs:
            totals[l["mode"]]+=l["seconds"]
        return {"departure":self.stamp(self.start),"arrival":self.stamp(cursor),
                "duration_seconds":int(cursor-self.start),"transfers":len(rides)-1,
                "walk_seconds":totals["walk"],"wait_seconds":totals["wait"],
                "access_walk_seconds":access["seconds"],"transfer_walk_seconds":transfer["seconds"] if transfer else 0,
                "egress_walk_seconds":egress["seconds"],
                "ride_seconds":totals["bus"],"legs":legs}
