"""Fresh, explicitly matched stop updates and optional official GPS observations."""

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256
import os
import math
from pathlib import Path
import re
from statistics import median
import sys
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .router import IST, approximate_m, coords


PUBLIC_TRIPS = "https://maps.reclaim.city/delhi/rt/trip_updates.pb"
PUBLIC_POSITIONS = "https://maps.reclaim.city/delhi/rt/vehicle_positions.pb"
OFFICIAL_POSITIONS = ("https://dts-backend.transportstack.in/api/dataset/otd/get-file"
                      "?agency=delhi-buses&category=realtime_gtfs&filename=VehiclePositions.pb")
TTL = 180
HORIZON = 90 * 60
MAX_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class VehicleRun:
    vehicle_id: str
    provider_trip: str
    route_id: str
    seen: int
    times: dict
    skipped: frozenset
    cancelled: bool
    live_run_id: str = ""
    direction_id: int | None = None


def protobuf():
    try:
        from google.transit import gtfs_realtime_pb2
    except ImportError:
        vendor = Path(__file__).resolve().parents[1] / ".runtime"
        if not vendor.is_dir():
            raise
        sys.path.insert(0,str(vendor))
        from google.transit import gtfs_realtime_pb2
    return gtfs_realtime_pb2


def download(url,key=None):
    headers = {"User-Agent":"DelhiTransitPlanner/1.0"}
    if key:
        headers["x-api-key"] = key
    with urlopen(Request(url,headers=headers), timeout=8) as response:
        data = response.read(MAX_BYTES+1)
    if not data or len(data) > MAX_BYTES:
        raise ValueError("Feed empty or above size limit")
    return protobuf().FeedMessage.FromString(data)


def fresh(ts,now):
    return ts > 0 and -30 <= now-ts <= TTL


class LiveOverlay:
    def __init__(self,eta,data_dir):
        self.eta, self.data_dir = eta, Path(data_dir)
        self.router = None
        self.ids, self.by_clock, self.edges = {}, defaultdict(list), defaultdict(set)
        self.trips_snapshot = (0,{})
        self.forecast_received = 0
        self.forecast_days = frozenset()
        self.board_index = (0,{})
        self.disruptions = (0,{})
        self.position_snapshot = (0,{})
        self.multiple_claims = (0, 0, {})
        self.single_vehicle_claims = (0, 0, {})
        self.vehicle_witnesses = (0, 0, {})
        self.exit_tracks = {}
        self.position_provider = None
        self.position_received = 0
        self.position_passages = ()
        self.tracks, self.observations = {}, defaultdict(list)
        self.feed_reports = {}
        self.last_attempt = None

    def bind(self,router):
        self.router = router
        self.ids.clear()
        self.by_clock.clear()
        self.edges.clear()
        for pid,p in enumerate(router.patterns):
            for k,trip in enumerate(p.trips):
                self.ids[trip] = (pid,k)
                self.by_clock[p.route,(p.starts[k]+p.departures[0])//60].append((pid,k))
            self.edges[p.route].update(zip(p.stops,p.stops[1:]))
        return self

    @classmethod
    def provider_clock(cls,route,run,start_time=None):
        """Parse a Delhi route/start clock; an optional numeric suffix stays part of the raw run identity."""
        prefix = route+'_'
        match = (re.fullmatch(r'([0-9]{1,2})_([0-5][0-9])(?:_[0-9]{1,4})?',run[len(prefix):])
                 if route and run.startswith(prefix) else None)
        if start_time:
            if match is None and cls.resembles_provider_clock(route,run):
                return None
            match = re.fullmatch(r'([0-9]{1,2}):([0-5][0-9])(?::[0-5][0-9])?',start_time)
        return int(match[1])*60+int(match[2]) if match else None

    @staticmethod
    def resembles_provider_clock(route,run):
        prefix = route+'_'
        return (run.startswith(prefix) and re.match(r'[0-9]+_',run[len(prefix):]) is not None
                or re.match(r'[^_]+_[0-9]+_',run) is not None)

    @classmethod
    def clock(cls,trip):
        return cls.provider_clock(trip.route_id,trip.trip_id,trip.start_time)

    def match(self,trip,calls,day):
        if not trip.trip_id or not calls:
            return None
        exact = self.ids.get(trip.trip_id)
        candidates = [exact] if exact is not None else self.by_clock.get((trip.route_id,self.clock(trip)),())
        matches=[]
        for pid,k in candidates:
            p=self.router.patterns[pid]
            if p.service not in self.router._active(day) or (trip.route_id and p.route!=trip.route_id):
                continue
            for i,s in enumerate(p.stops):
                if s==calls[0] and p.stops[i:i+len(calls)]==calls:
                    matches.append((p,k,i))
                    if len(matches)>1:
                        return None
        return matches[0] if len(matches)==1 else None

    def match_disruption(self,trip,updates,day):
        """Resolve sparse calls by their actual GTFS sequence or a unique stop ID."""
        exact=self.ids.get(trip.trip_id)
        candidates=[exact] if exact is not None else self.by_clock.get((trip.route_id,self.clock(trip)),())
        matches=[]
        for pid,k in candidates:
            p=self.router.patterns[pid]
            if p.service not in self.router._active(day) or (trip.route_id and trip.route_id!=p.route):
                continue
            positions=[]
            for u in updates:
                if u.HasField("stop_sequence"):
                    if u.stop_sequence not in p.sequences:
                        break
                    i=p.sequences.index(u.stop_sequence)
                    if u.stop_id and u.stop_id!=p.stops[i]:
                        break
                elif u.stop_id and p.stops.count(u.stop_id)==1:
                    i=p.stops.index(u.stop_id)
                else:
                    break
                if positions and i<=positions[-1]:
                    break
                positions.append(i)
            else:
                matches.append((p,k,positions))
        return matches[0] if len(matches)==1 else None

    def apply_trip_updates(self,feed,now=None):
        now = int(time.time() if now is None else now)
        stamp = int(feed.header.timestamp)
        if not fresh(stamp,now):
            return {"state":"stale","timestamp":stamp,"matched_trips":0}
        if feed.header.incrementality != 0:
            return {"state":"unsupported_incremental_feed","timestamp":stamp,"matched_trips":0}
        mapped, restrictions, claims, sample = {},{},defaultdict(list),None
        entities = 0
        for entity in feed.entity:
            if not entity.HasField("trip_update") or entity.is_deleted:
                continue
            entities+=1
            update = entity.trip_update
            trip = update.trip
            seen = int(update.timestamp or stamp)
            cancelled = trip.schedule_relationship == trip.CANCELED
            if not fresh(seen,now) or (trip.schedule_relationship != 0 and not cancelled):
                continue
            try:
                day = datetime.strptime(trip.start_date,"%Y%m%d").date()
            except ValueError:
                continue
            if abs((day-datetime.fromtimestamp(now,IST).date()).days)>1:
                continue
            stops = [u.stop_id for u in update.stop_time_update]
            has_skips = any(u.schedule_relationship==u.SKIPPED for u in update.stop_time_update)
            if cancelled or has_skips:
                match=self.match_disruption(trip,update.stop_time_update,day)
            else:
                if not stops or len(set(stops))!=len(stops):
                    continue
                seq = [u.stop_sequence for u in update.stop_time_update if u.HasField("stop_sequence")]
                if any(b<=a for a,b in zip(seq,seq[1:])):
                    continue
                block=self.match(trip,stops,day)
                match=(block[0],block[1],range(block[2],block[2]+len(stops))) if block else None
            if match is None:
                continue
            p,k,positions = match
            key = day,p.trips[k]
            skipped=frozenset(i for i,u in zip(positions,update.stop_time_update)
                              if u.schedule_relationship==u.SKIPPED)
            times,previous,previous_stop = {},0,None
            midnight = int(datetime(day.year,day.month,day.day,tzinfo=IST).timestamp())
            for i,u in zip(positions,update.stop_time_update):
                if u.schedule_relationship != 0:
                    continue
                # departure() returns an epoch departure; add scheduled dwell to an arrival-only event.
                dwell = p.departures[i]-p.arrivals[i]
                nominal = midnight+p.starts[k]+p.departures[i]
                if u.HasField("departure") and u.departure.HasField("time"):
                    prediction = int(u.departure.time)
                elif u.HasField("arrival") and u.arrival.HasField("time"):
                    prediction = int(u.arrival.time)+dwell
                elif u.HasField("departure") and u.departure.HasField("delay"):
                    prediction = nominal+u.departure.delay
                elif u.HasField("arrival") and u.arrival.HasField("delay"):
                    prediction = nominal+u.arrival.delay
                else:
                    continue
                if prediction < previous:
                    times.clear()
                    break
                if previous_stop is not None:
                    distance = approximate_m(coords(self.router.stops[previous_stop]),coords(self.router.stops[p.stops[i]]))
                    if distance > 60 and prediction - previous < distance / 35:
                        times.clear()
                        break
                previous = prediction
                previous_stop = p.stops[i]
                if (seen-30 <= prediction <= seen+HORIZON and abs(prediction-nominal)<=HORIZON
                        and p.stops.count(p.stops[i])==1):
                    times[p.stops[i]] = prediction
            # A vehicle-specific match needs an explicit route and its own observation clock.
            # Stop assignments and trip-property overrides require a different served pattern.
            split_eligible = (trip.route_id and update.timestamp and not update.HasField("trip_properties")
                              and not any(u.HasField("stop_time_properties") for u in update.stop_time_update))
            vehicle_id = update.vehicle.id.strip() if split_eligible else ""
            direction = trip.direction_id if trip.HasField("direction_id") else None
            claims[key].append(VehicleRun(vehicle_id,trip.trip_id,p.route,seen,times,skipped,cancelled,direction_id=direction))
        multiple, singles = {}, {}
        for key, rows in claims.items():
            # When a physical run cannot be identified, keep the protective restrictions.
            if any(r.cancelled for r in rows):
                restrictions[key] = max(r.seen for r in rows if r.cancelled),None
            elif any(r.skipped for r in rows):
                restrictions[key] = max(r.seen for r in rows if r.skipped),frozenset().union(*(r.skipped for r in rows))
            if len(rows) == 1:
                row = rows[0]
                if row.times and not row.cancelled:
                    mapped[key] = row.times,row.seen
                    if row.vehicle_id:
                        singles[key] = row.seen, row.vehicle_id, row.provider_trip
            elif all(r.vehicle_id for r in rows) and len({r.vehicle_id for r in rows}) == len(rows):
                day,trip = key
                multiple[key] = tuple(replace(r,live_run_id=sha256(
                    f"{day}/{trip}/{r.vehicle_id}".encode()).hexdigest()[:24]) for r in rows)
        self.multiple_claims = stamp,now,multiple
        self.single_vehicle_claims = stamp,now,singles
        predictions = {(day,trip,stop): (event,seen) for (day,trip),(times,seen) in mapped.items()
                       for stop,event in times.items()}
        boards = defaultdict(list)
        for (day,trip,stop),(event,_) in predictions.items():
            pid,k=self.ids[trip]
            boards[(day,pid,stop)].append((event,k))
        self.board_index=stamp,boards
        self.forecast_received = now
        self.forecast_days = frozenset(day for day,_ in mapped.keys() | multiple.keys())
        self.trips_snapshot = stamp,predictions
        self.disruptions=stamp,restrictions
        if mapped:
            (day,trip),(times,_)=next(iter(mapped.items()))
            sample={"trip_id":trip,"service_date":day.isoformat(),"stop_id":next(iter(times))}
        return {"state":"fresh" if mapped or restrictions else "fresh_no_match","timestamp":stamp,"entities":entities,
                "matched_trips":len(mapped.keys() | restrictions.keys()),"matched_stop_updates":len(predictions),
                "cancelled_trips":sum(r is None for _,r in restrictions.values()),
                "skipped_stop_calls":sum(len(r) for _,r in restrictions.values() if r is not None),
                "multi_vehicle_trip_candidates":len(multiple),
                "example":sample,"source":"Reclaim Delhi GTFS-Realtime TripUpdates"}

    def apply_positions(self,feed,now=None,provider="official_positions"):
        now = int(time.time() if now is None else now)
        stamp = int(feed.header.timestamp)
        if not fresh(stamp,now):
            return {"state":"stale","timestamp":stamp}
        matched, observations = 0,0
        witnesses = {}
        seen_ids, duplicate_ids = set(),set()
        for entity in feed.entity:
            if entity.HasField('vehicle') and not entity.is_deleted:
                vid = entity.vehicle.vehicle.id
                if vid in seen_ids:
                    duplicate_ids.add(vid)
                seen_ids.add(vid)
        for vid in duplicate_ids:
            witnesses[vid] = None
            self.tracks.pop(vid,None)
        for entity in feed.entity:
            if not entity.HasField("vehicle") or entity.is_deleted:
                continue
            v = entity.vehicle
            rt,run,vid = v.trip.route_id,v.trip.trip_id,v.vehicle.id
            observed = int(v.timestamp)
            if not (vid and vid not in duplicate_ids and fresh(observed,now) and observed <= now):
                continue
            if rt not in self.edges or not run or not v.HasField('position'):
                witnesses[vid] = None
                self.tracks.pop(vid,None)
                continue
            coord = v.position.latitude,v.position.longitude
            if not (-90 <= coord[0] <= 90 and -180 <= coord[1] <= 180):
                witnesses[vid] = None
                continue
            day = v.trip.start_date or None
            if day:
                direction = v.trip.direction_id if v.trip.HasField("direction_id") else None
                witnesses[vid] = (day,rt,run,observed,coord,direction)
            else:
                witnesses[vid] = None
            track = self.tracks.get(vid)
            if track and observed <= track[4]:
                continue
            start_clock = v.trip.start_time or None
            if track and (track[:2]!=(rt,run) or track[5]!=day or start_clock and track[7] and start_clock!=track[7]):
                self.tracks.pop(vid)
                track = None
            near = [(d,s) for d,s in self.router._nearby(coord,45)
                    if any(self.router.patterns[p].route==rt for p,_ in self.router.at[s])]
            if not near or (len(near)>1 and near[1][0]-near[0][0]<12):
                continue
            s = near[0][1]
            matched+=1
            first = observed
            if track and track[:2]==(rt,run) and observed-track[4]<=240:
                if s==track[2]:
                    first = track[3]
                elif (track[2],s) in self.edges[rt]:
                    delta = observed-track[3]
                    distance = approximate_m(coords(self.router.stops[s]),coords(self.router.stops[track[2]]))
                    if 15<=delta<=900 and distance/delta<23:
                        self.observations[rt,track[2],s].append((observed,delta))
                        observations+=1
            self.tracks[vid] = rt,run,s,first,observed,day,now,start_clock or (track[7] if track else None)
        self.tracks={v:t for v,t in self.tracks.items() if now-t[4]<=900}
        recent = {}
        for key in list(self.observations):
            values=[v for v in self.observations[key] if now-v[0]<=1800]
            if values:
                self.observations[key]=values
                recent[key] = (median(d for _,d in values),len(values),max(t for t,_ in values))
            else:
                del self.observations[key]
        self.position_provider = provider
        self.update_exit_tracks(witnesses, now)
        self.vehicle_witnesses = stamp,now,witnesses
        self.position_received = now
        self.position_passages = tuple({"route_id":rt, "stop_id":stop, "timestamp":first, "received_at":received,
                                        "position_trip":run, "position_vehicle":vid,
                                        "service_date":day,"position_start_time":start_clock}
                                       for vid,(rt, run, stop, first, _,day,received,start_clock) in self.tracks.items()
                                       if 0 <= now - first <= TTL)
        self.position_snapshot = stamp,recent
        return {"state":"fresh","timestamp":stamp,"entities":len(feed.entity),"matched_positions":matched,
                "new_segment_samples":observations,"segments_available":len(recent),
                "source":("Transport Stack official DTC/DoT vehicle positions" if provider == "official_positions"
                          else "Reclaim Delhi GTFS-Realtime VehiclePositions")}

    def update_exit_tracks(self, witnesses, receipt):
        history = {key:(ident,points) for key,(ident,points) in self.exit_tracks.items() if points[-1][0]>=receipt-TTL}
        for vehicle, witness in witnesses.items():
            if witness is None:
                history.pop(vehicle,None)
                continue
            day,route,trip,observed,location,direction = witness
            if not 0 <= receipt-observed <= TTL:
                continue
            ident = day,route,trip,direction
            old_ident,points = history.get(vehicle,(None,()))
            if points:
                last,_,previous = points[-1]
                if observed<last:
                    continue
                if observed==last:
                    if ident!=old_ident or location!=previous:
                        history.pop(vehicle,None)
                    continue
                delta = observed-last
                if ident!=old_ident or delta>60 or approximate_m(previous,location)>35*delta:
                    points = ()
            history[vehicle] = ident,tuple(p for p in points if p[0]>=receipt-TTL)+((observed,receipt,location),)
        self.exit_tracks = history

    def boarding_track(self, day, pid, identity, asof):
        if not self.eligible(self.position_snapshot[0],asof) or self.position_received>asof:
            return ()
        vehicle,raw_trip = identity
        pattern = self.router.patterns[pid]
        found = self.exit_tracks.get(vehicle)
        if not found or found[0][:3]!=(day.strftime('%Y%m%d'),pattern.route,raw_trip):
            return ()
        return tuple(p for p in found[1] if asof-TTL <= p[0] <= p[1] <= asof)

    def boarding_position(self, day, pid, index, identity, asof):
        """Age of dated vehicle GPS and a positive, recently observed approach on the inbound shape."""
        points = self.boarding_track(day,pid,identity,asof)
        if not points:
            return None,False
        age = asof-points[-1][0]
        pattern = self.router.patterns[pid]
        if age > 60 or len(points)<2 or index==0 or pattern.stops.count(pattern.stops[index])!=1:
            return age,False
        path = self.router.platform_path(pid,index)
        if not path:
            return age,False
        old,latest = (path.project(p[2],max_offset=45) for p in points[-2:])
        return age,(old is not None and latest is not None and latest < -45 and latest-old >= 20)

    def passed_platform(self, day, pid, index, identity, asof):
        """A dated physical bus entered the platform area and exited forward before the request."""
        points = self.boarding_track(day,pid,identity,asof)
        pattern = self.router.patterns[pid]
        if len(points)<3 or index+1>=len(pattern.stops):
            return False
        stop,next_stop = pattern.stops[index:index+2]
        if pattern.stops.count(stop)!=1 or pattern.stops.count(next_stop)!=1:
            return False
        a,b = coords(self.router.stops[stop]),coords(self.router.stops[next_stop])
        length = approximate_m(a,b)
        if length<100:
            return False
        if not any(approximate_m(a,pt)<=46 for _,_,pt in points[1:-1]):
            return False
        lon_scale = 111195*math.cos(math.radians((a[0]+b[0])/2))
        dx,dy = (b[1]-a[1])*lon_scale,(b[0]-a[0])*111195
        entered = exited = False
        last_distance = last_along = None
        path = self.router.platform_path(pid,index)
        previous_shape = entry_shape = None
        for _,_,pt in points:
            x,y = (pt[1]-a[1])*lon_scale,(pt[0]-a[0])*111195
            distance = math.hypot(x,y)
            along = (x*dx + y*dy)/length
            shape = path.project(pt) if path else None
            if distance<=45:
                exited = False
                clear = all(other==stop or approximate_m(pt,coords(self.router.stops[other]))>=distance+12
                            for other in pattern.stops)
                entered = clear and (entered or last_distance is not None and last_distance>45)
                # A directed shape exit also needs a preceding fix on the inbound road.
                if not clear or shape is None:
                    entry_shape = None
                elif entry_shape is not None or last_distance is not None and last_distance>45 and previous_shape is not None and previous_shape < -20:
                    entry_shape = shape
            elif entered:
                cross = abs(x*dy - y*dx)/length
                exited = 20<=along<=length+45 and along-last_along>=20 and cross<=75
                entered = False
            if distance>45 and entry_shape is not None:
                if shape is None or shape < entry_shape:
                    entry_shape = None
                elif shape>=20 and shape-entry_shape>=20:
                    exited = True
                    entry_shape = None
            last_distance,last_along = distance,along
            previous_shape = shape
        return exited

    def fetch(self):
        if self.router is None:
            raise RuntimeError("Bind the live overlay to the static router first.")
        now = int(time.time())
        key = os.getenv("DELHI_GTFS_RT_KEY")
        streams=[("community_trip_updates",PUBLIC_TRIPS,None,self.apply_trip_updates)]
        if key:
            streams.append(("official_positions",OFFICIAL_POSITIONS,key,self.apply_positions))
        else:
            streams.append(("community_positions",PUBLIC_POSITIONS,None,
                            lambda feed: self.apply_positions(feed,provider="community_positions")))
        reports = {}
        for name,url,secret,apply in streams:
            try:
                reports[name]=apply(download(url,secret))
            except HTTPError as exc:
                reports[name]={"state":"unavailable","http_status":exc.code}
            except Exception as exc:
                reports[name]={"state":"unavailable","error":type(exc).__name__}
        self.last_attempt=now
        self.feed_reports=reports
        return self.status()

    def status(self, asof=None):
        now = int(time.time()) if asof is None else asof
        ts,trips=self.trips_snapshot
        ps,segments=self.position_snapshot
        exclusions=self.trip_exclusions(now)
        multi = self.vehicle_runs(now)
        vehicle_count = sum(len(rows) for rows in multi.values())
        vehicle_calls = sum(len(row.times) for rows in multi.values() for row in rows)
        enabled = bool(fresh(ts,now) and trips or fresh(ps,now) and segments or exclusions or vehicle_count)
        report_times = [s.get("timestamp",0) for s in self.feed_reports.values()]
        timestamp = max([ts,ps,0]+report_times)
        state = "fresh" if enabled else "unavailable"
        if not enabled and timestamp:
            state = "fresh_no_match" if fresh(timestamp,now) else "stale"
        return {"state":state,"timestamp":timestamp or None,"source":"GTFS-Realtime",
                "usable":enabled,"last_attempt":self.last_attempt,"max_age_seconds":TTL,
                "matched_stop_updates":(len(trips) if fresh(ts,now) else 0) + vehicle_calls,
                "corroborated_vehicle_runs":vehicle_count,
                "cancelled_trips":sum(s is None for s in exclusions.values()),
                "skipped_stop_calls":sum(len(s) for s in exclusions.values() if s is not None),
                "observed_segments":len(segments) if fresh(ps,now) else 0,
                "position_provider":self.position_provider,
                "official_observed_segments":len(segments) if fresh(ps,now) and self.position_provider=="official_positions" else 0,
                "sources":self.feed_reports}

    @staticmethod
    def eligible(stamp,asof):
        # Require a recent feed and an issue clock contemporaneous with the caller.
        return stamp and stamp<=asof and asof-stamp<=TTL and abs(time.time()-asof)<=TTL

    def trip_exclusions(self,asof):
        """None means the trip is canceled. A set identifies skipped pattern call indexes."""
        stamp,rows=self.disruptions
        if not rows or self.forecast_received>asof or not self.eligible(stamp,asof):
            return {}
        return {key:skips for key,(seen,skips) in rows.items() if seen<=asof and asof-seen<=TTL}

    def vehicle_runs(self, asof):
        """Return separately corroborated physical runs only for multiply claimed dated trips."""
        ts, received, claims = self.multiple_claims
        ps, positions_received, witnesses = self.vehicle_witnesses
        if not claims or max(received,positions_received) > asof or not self.eligible(ts,asof) or not self.eligible(ps,asof):
            return {}
        result = {}
        for (day,trip), rows in claims.items():
            confirmed = []
            for row in rows:
                witness = witnesses.get(row.vehicle_id)
                if not witness or witness[:3] != (day.strftime('%Y%m%d'),row.route_id,row.provider_trip):
                    break
                when,location,direction = witness[3:]
                if row.direction_id is not None and direction is not None and row.direction_id != direction:
                    break
                if not (0 <= asof - row.seen <= TTL and 0 <= asof - when <= TTL and abs(when - row.seen) <= 90):
                    break
                if row.cancelled:
                    continue
                if not row.times:
                    break
                first = next(iter(row.times))
                # A distant position cannot corroborate the first call on this vehicle's forecast.
                plausible_distance = min(3000, 300 + 35 * abs(row.times[first] - when))
                if approximate_m(location, coords(self.router.stops[first])) > plausible_distance:
                    break
                confirmed.append(row)
            else:
                result[day,trip] = tuple(confirmed)
        return result

    def forecast_vehicle(self,day,trip,asof):
        """Vehicle and raw trip ID named by this dated, fresh, single-vehicle forecast."""
        stamp,received,claims = self.single_vehicle_claims
        row = claims.get((day,trip))
        if row and received <= asof and self.eligible(stamp,asof) and 0 <= asof-row[0] <= TTL:
            return row[1:]
        return None

    def recent_passages(self, when):
        issue = int(when.timestamp())
        if self.eligible(self.position_snapshot[0], issue) and self.position_received <= issue:
            return [p for p in self.position_passages if p["timestamp"] >= issue - TTL]
        return self.eta.recent_passages(when)

    def can_approach(self, passage, stop, when):
        """Honor known trip-specific stopping patterns and disruptions for raw positions."""
        run = passage.get("position_trip")
        if not run:
            return True
        rt = passage["route_id"]
        exact = self.ids.get(run)
        if exact:
            candidates = [exact]
        else:
            start_clock = passage.get('position_start_time')
            minute = self.provider_clock(rt,run,start_clock)
            if minute is None:
                return not start_clock and not self.resembles_provider_clock(rt,run)
            candidates = self.by_clock.get((rt,minute),())
            if not candidates or not passage.get('service_date'):
                return False
        day = when.date()
        if passage.get('service_date'):
            try:
                day = datetime.strptime(passage['service_date'],'%Y%m%d').date()
            except (TypeError,ValueError):
                return False
        if abs((day-when.date()).days)>1:
            return False
        active = self.router._active(day)
        # Resolve only with facts already observed. The requested downstream stop is not evidence
        # that the vehicle follows the longer of two possible stopping patterns.
        candidates = [(pid,k) for pid,k in candidates if self.router.patterns[pid].route==rt
                      and self.router.patterns[pid].service in active and passage['stop_id'] in self.router.patterns[pid].stops]
        if len(candidates)!=1 or self.router.patterns[candidates[0][0]].stops.count(passage['stop_id'])!=1:
            return False
        asof = int(when.timestamp())
        pid,k = candidates[0]
        pattern = self.router.patterns[pid]
        key = day,pattern.trips[k]
        skips = self.trip_exclusions(asof).get(key,())
        runs = self.vehicle_runs(asof)
        if key in runs:
            match = next((row for row in runs[key] if row.vehicle_id==passage.get('position_vehicle')
                          and row.provider_trip==run),None)
            if match is None or stop not in match.times:
                return False
            skips = match.skipped
        first = pattern.stops.index(passage['stop_id'])+1
        return skips is not None and any(s==stop and not pattern.pickups[i] and i not in skips
                                         for i,s in enumerate(pattern.stops[first:],first))

    def departure(self,trip_id,stop_id,service_date,scheduled_epoch,asof_epoch):
        event = self.boarding_departure(trip_id,stop_id,service_date,scheduled_epoch,asof_epoch)
        return event if event is not None and event >= asof_epoch-30 else None

    def boarding_departure(self,trip_id,stop_id,service_date,scheduled_epoch,asof_epoch):
        """A current forecast still rules out timetable boarding after its predicted clock has passed."""
        stamp,lookup=self.trips_snapshot
        if self.forecast_received>asof_epoch or not self.eligible(stamp,asof_epoch):
            return None
        row=lookup.get((service_date,trip_id,stop_id))
        if row and row[1]<=asof_epoch and asof_epoch-row[1]<=TTL and row[0]<=asof_epoch+HORIZON:
            return row[0]
        return None

    def live_candidates(self,pid,stop,day,ready_epoch,latest_epoch,asof_epoch):
        # Large observed delays can bring a nominally much earlier trip back into consideration.
        stamp,index=self.board_index
        if self.forecast_received>asof_epoch or not self.eligible(stamp,asof_epoch):
            return ()
        return [k for event,k in index.get((day,pid,stop),()) if ready_epoch<=event<=latest_epoch]

    def forecast_service_dates(self,asof):
        return self.forecast_days if self.forecast_received<=asof and self.eligible(self.trips_snapshot[0],asof) else ()

    def duration(self,route_id,from_stop,to_stop,service_second,scheduled_seconds,asof=None,service_date=None):
        estimate,source=self.eta.duration(route_id,from_stop,to_stop,service_second,scheduled_seconds,
                                         asof=asof,service_date=service_date)
        stamp,segments=self.position_snapshot
        sample = segments.get((str(route_id),str(from_stop),str(to_stop)))
        if sample is None or not stamp or asof is None:
            return estimate,source
        issue = int(asof.timestamp()) if isinstance(asof,datetime) else int(asof)
        if not self.eligible(stamp,issue) or self.position_received > issue:
            return estimate,source
        issue_dt=datetime.fromtimestamp(issue,IST)
        day=service_date or issue_dt.date()
        start_epoch=datetime(day.year,day.month,day.day,tzinfo=IST).timestamp()+service_second
        delta=start_epoch-issue
        if -1800<=delta<=HORIZON and sample[2]<=issue:
            value,n,_=sample
            weight=n/(n+3)
            return max(1,round(estimate*(1-weight)+value*weight)),"gps_live_segment"
        return estimate,source
