"""Report an observed physical movement separately from platform waits and named boarding."""
from datetime import date,datetime,timedelta
import gzip
import time
from typing import Literal

from pydantic import AwareDatetime,BaseModel,Field

from .contract import Place,PlatformEstimate
from .live import LiveOverlay,TTL,fresh,protobuf
from .replay import received_files
from .router import IST,approximate_m,coords


class RouteObservation(BaseModel):
    route_id: str
    route: str
    observed_stop: Place
    observed_at: AwareDatetime
    received_at: AwareDatetime
    age_seconds: int = Field(ge=0,le=60,description="Vehicle GPS age at the request.")
    service_date: date
    direction: Literal['toward_platform']
    trip_assignment: Literal['unmatched_start_clock']


class PlatformResponse(PlatformEstimate):
    route_observation: RouteObservation | None = Field(default=None,description=(
        "Separately observed movement at the preceding stop. The vehicle has no matched timetable trip."))


def plausible_service_days(router,when):
    start=(when-when.replace(hour=0,minute=0,second=0,microsecond=0)).total_seconds()
    day=when.date()
    days=[day]
    if router.max_service+5400>=86400+start:
        days.append(day-timedelta(days=1))
    if start+5400>=86400:
        days.append(day+timedelta(days=1))
    return tuple(days)


def unanimous_paths(router,route,origin,target,days):
    """Every active pattern through the witness platform must serve the very next stop for boarding."""
    active=set.union(*(router._active(d) for d in days))
    paths=[]
    for pid,i in router.at[origin]:
        p=router.patterns[pid]
        if p.route!=route or p.service not in active:
            continue
        if (p.stops.count(origin)!=1 or p.stops.count(target)!=1 or i+2>=len(p.stops)
                or p.stops[i+1]!=target or p.pickups[i+1]):
            return ()
        paths.append((pid,i,i+1))
    return tuple(paths)


def directed_observation(router,paths,origin,points,issue):
    if len(points)<2 or not paths:
        return False
    (a,ar,old),(b,br,latest)=points[-2:]
    if (not a<=ar<=issue or not b<=br<=issue or not 0<b-a<=60 or not 0<=issue-b<=60 or
            approximate_m(old,latest)>35*(b-a) or approximate_m(latest,coords(router.stops[origin]))>45):
        return False
    for pid,_,j in paths:
        shape=router.platform_path(pid,j)
        if shape is None:
            return False
        before,after=shape.project(old,max_offset=45),shape.project(latest,max_offset=45)
        if before is None or after is None or after>=-45 or after-before<20:
            return False
    return True


def remember_restrictions(overlay,feed,received):
    stamp=int(feed.header.timestamp)
    if not fresh(stamp,received):
        return
    if feed.header.incrementality!=0:
        overlay.route_restrictions=(stamp,received,None)
        return
    flags=[]
    for e in feed.entity:
        if e.is_deleted or not e.HasField('trip_update'):
            continue
        update=e.trip_update
        trip=update.trip
        changed=[u for u in update.stop_time_update if u.schedule_relationship!=0 or u.HasField('stop_time_properties')]
        canceled=trip.schedule_relationship in (trip.CANCELED,getattr(trip,'DELETED',7))
        if not canceled and not changed and not update.HasField('trip_properties'):
            continue
        # Sparse sequence-only restrictions and modified trip descriptors can change the stopping pattern.
        whole=canceled or update.HasField('trip_properties') or any(
            not u.stop_id or u.HasField('stop_time_properties') for u in changed)
        affected=None if whole else frozenset(u.stop_id for u in changed)
        static=overlay.ids.get(trip.trip_id)
        route=trip.route_id or (overlay.router.patterns[static[0]].route if static else None)
        flags.append((int(update.timestamp or stamp),trip.start_date,route,trip.trip_id,update.vehicle.id,affected))
    overlay.route_restrictions=(stamp,received,tuple(flags))


class ObservationOverlay(LiveOverlay):
    def apply_trip_updates(self,feed,now=None):
        receipt=int(time.time() if now is None else now)
        remember_restrictions(self,feed,receipt)
        return super().apply_trip_updates(feed,receipt)


def recorded_restrictions(overlay,directory,issue):
    if getattr(overlay,'route_restrictions_restored',False):
        return
    for receipt,kind,path in received_files(directory/'nov21-samples'):
        if issue-TTL<=receipt<=issue and kind=='predictions':
            with gzip.open(path,'rb') as src:
                remember_restrictions(overlay,protobuf().FeedMessage.FromString(src.read()),receipt)
    overlay.route_restrictions_restored=True


def restricted(overlay,route,identity,days,origin,target,issue):
    stamp,receipt,flags=getattr(overlay,'route_restrictions',(0,0,()))
    if receipt<=issue and overlay.eligible(stamp,issue):
        if flags is None:
            return True
        possible_dates={d.strftime('%Y%m%d') for d in days}
        vehicle,raw=identity
        for seen,day,affected_route,trip,vid,stops in flags:
            if not 0<=issue-seen<=TTL or day and day not in possible_dates:
                continue
            if affected_route==route or trip==raw or vid and vid==vehicle:
                if stops is None or origin in stops or target in stops:
                    return True
    return False


def route_observation(router,overlay,stop,route,when,answer):
    if answer.get('possible_boarding') or answer['source'] in ('upstream_gps','live','live_inferred','gps_live_segment'):
        return None
    issue=int(when.timestamp())
    stamp,received,witnesses=getattr(overlay,'vehicle_witnesses',(0,0,{}))
    if not stamp:
        return None
    if received>issue or not overlay.eligible(stamp,issue):
        return None
    days=plausible_service_days(router,when)
    results=[]
    for passage in overlay.position_passages:
        origin=passage['stop_id']
        if passage['route_id']!=route or origin==stop or not passage['timestamp']<=passage['received_at']<=issue:
            continue
        raw,vehicle=passage['position_trip'],passage['position_vehicle']
        minute=overlay.provider_clock(route,raw,passage.get('position_start_time'))
        if raw in overlay.ids or minute is None or overlay.by_clock.get((route,minute)):
            continue
        witness=witnesses.get(vehicle)
        if not witness or witness[:3]!=(passage['service_date'],route,raw):
            continue
        try:
            day=datetime.strptime(passage['service_date'],'%Y%m%d').date()
        except (TypeError,ValueError):
            continue
        paths=unanimous_paths(router,route,origin,stop,days)
        if day not in days or not any(router.patterns[pid].service in router._active(day) for pid,_,_ in paths):
            continue
        identity=vehicle,raw
        if restricted(overlay,route,identity,days,origin,stop,issue):
            continue
        points=overlay.boarding_track(day,paths[0][0],identity,issue)
        if not points or points[-1][0]!=witness[3] or not directed_observation(router,paths,origin,points,issue):
            continue
        observed,arrived,_=points[-1]
        # The geofence passage must still refer to this specific latest GPS fix.
        track=overlay.tracks.get(vehicle)
        if not track or track[:3]!=(route,raw,origin) or track[4]!=observed or track[5]!=passage['service_date']:
            continue
        results.append((observed,arrived,origin,day))
    if not results:
        return None
    seen,receipt,origin,day=max(results)
    return {'route_id':route,'route':router.routes[route][0],'observed_stop':router.stops[origin],
            'observed_at':datetime.fromtimestamp(seen,IST).isoformat(),'received_at':datetime.fromtimestamp(receipt,IST).isoformat(),
            'age_seconds':issue-seen,'service_date':day.isoformat(),'direction':'toward_platform',
            'trip_assignment':'unmatched_start_clock'}
