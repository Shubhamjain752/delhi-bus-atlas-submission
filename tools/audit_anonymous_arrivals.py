"""Six input-selected development checks of the route-only /arrivals fallback."""
import argparse
from datetime import datetime,timedelta
import importlib
from itertools import groupby
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tools.audit_provider_clocks import DAYS,WINDOWS
from tools.replay_fleet_boarding import baseline_package
from tools.train_eta import boundary,open_db
from transit.arrivals import platform_estimate
from transit.api import app,arrivals
from transit.eta import EtaModel
from transit.live import protobuf
from transit.replay import CapturedOverlay
from transit.route_observation import PlatformResponse
from transit.router import IST,Router,approximate_m,coords


def candidate(db,day,window,router,registry):
    lo,hi = (boundary(db,value) for value in window)
    for vehicle,raw,rt,stamp,clock,lat,lon in db.execute(
            'SELECT vehicle_id,trip_id,route_id,timestamp,time,lat,lng FROM vehicle_feed '
            'WHERE rowid>=? AND rowid<? ORDER BY rowid',(lo,hi)):
        if not (vehicle and raw and rt is not None and stamp and lat and lon):
            continue
        route,raw,stamp = str(int(rt)),str(raw),int(stamp)
        minute = registry.provider_clock(route,raw)
        if minute is None or registry.by_clock.get((route,minute)):
            continue
        when = datetime.fromisoformat(day+'T'+clock).replace(tzinfo=IST)
        if not 0<=when.timestamp()-stamp<=90:
            continue
        near = [(d,s) for d,s in router._nearby((float(lat),float(lon)),45)
                if any(router.patterns[p].route==route for p,_ in router.at[s])]
        if not near or len(near)>1 and near[1][0]-near[0][0]<12:
            continue
        origin = near[0][1]
        # Select on static geography and archived inputs only, before computing either answer.
        targets = {p.stops[i+1] for pid,i in router.at[origin] for p in [router.patterns[pid]]
                   if p.route==route and p.service in router._active(when.date()) and i+2<len(p.stops)
                   and not p.pickups[i+1] and p.stops.count(origin)==1 and p.stops.count(p.stops[i+1])==1}
        if len(targets)==1:
            return vehicle,raw,route,stamp,when,float(lat),float(lon),origin,targets.pop()
    return None


def arrival_bracket(db,router,vehicle,raw,route,stamp,when,location,target):
    issue = int(when.timestamp())
    lo,hi = (boundary(db,t.strftime('%H:%M:%S')) for t in (when,when+timedelta(minutes=20)))
    last,previous = stamp,location
    for t,rt,seen,wall,y,x in db.execute('SELECT trip_id,route_id,timestamp,time,lat,lng FROM vehicle_feed '
            'WHERE rowid>=? AND rowid<? AND vehicle_id=? ORDER BY rowid',(lo,hi,vehicle)):
        if not (seen and wall and y and x):
            continue
        observed = int(seen)
        received = int(datetime.fromisoformat(when.date().isoformat()+'T'+wall).replace(tzinfo=IST).timestamp())
        if not 0<=received-observed<=90 or observed<last:
            continue
        point = float(y),float(x)
        if str(t)!=raw or rt is None or str(int(rt))!=route:
            return {'outcome':'run_changed'}
        if observed==last:
            if approximate_m(previous,point)>1:
                return {'outcome':'conflicting_coordinates'}
            continue
        if observed-last>60 or approximate_m(previous,point)>35*(observed-last):
            return {'outcome':'gps_continuity_lost'}
        if approximate_m(point,coords(router.stops[target]))<=45:
            near=[(d,s) for d,s in router._nearby(point,45)
                  if any(router.patterns[p].route==route for p,_ in router.at[s])]
            if (not near or near[0][1]!=target or len(near)>1 and near[1][0]-near[0][0]<12
                    or approximate_m(previous,coords(router.stops[target]))<=45):
                return {'outcome':'ambiguous_entry'}
            return {'outcome':'entry_observed','wait_lower_s':last-issue,'wait_upper_s':observed-issue,
                    'received_after_issue_s':received-issue}
        previous,last = point,observed
    return {'outcome':'no_entry_within_20_minutes'}


def agreement(router,route,origin,target,when):
    dates = [when.date()]
    midnight = when.replace(hour=0,minute=0,second=0,microsecond=0)
    if router.max_service+5400>=86400+(when-midnight).total_seconds():
        dates.append(when.date()-timedelta(days=1))
    active = set.union(*(router._active(d) for d in dates))
    patterns = {pid for pid,_ in router.at[origin] if router.patterns[pid].route==route
                and router.patterns[pid].service in active}
    paths,invalid = set(),0
    for pid in patterns:
        p = router.patterns[pid]
        if p.stops.count(origin)!=1 or p.stops.count(target)!=1:
            invalid+=1
            continue
        i,j = p.stops.index(origin),p.stops.index(target)
        if not i<j<len(p.stops)-1 or p.pickups[j]:
            invalid+=1
            continue
        paths.add(tuple(p.stops[i:j+1]))
    return {'active_patterns_through_observed_stop':len(patterns),
            'incompatible_or_not_boardable':invalid,'different_ordered_paths':len(paths),
            'all_active_patterns_agree':bool(patterns and not invalid and len(paths)==1),
            'disruption_evidence':'No TripUpdates or operator incident data in these position-only snapshots.'}


def archived_endpoint(db,router,registry,vehicle,when,route,target):
    issue=int(when.timestamp())
    live=CapturedOverlay(router.eta,ROOT/'data',issue)
    for key in ('router','ids','by_clock','edges'):
        setattr(live,key,getattr(registry,key))
    lo,hi=(boundary(db,t.strftime('%H:%M:%S')) for t in (when-timedelta(seconds=180),when+timedelta(seconds=1)))
    records=db.execute('select time,route_id,trip_id,timestamp,lat,lng from vehicle_feed where rowid>=? and rowid<? '
                       'and vehicle_id=? order by rowid',(lo,hi,vehicle))
    for wall,group in groupby(records,key=lambda p:p[0]):
        received=int(datetime.fromisoformat(when.date().isoformat()+'T'+wall).replace(tzinfo=IST).timestamp())
        feed=protobuf().FeedMessage()
        feed.header.gtfs_realtime_version,feed.header.timestamp='2.0',received
        for i,(_,rt,raw,observed,lat,lon) in enumerate(group):
            if None in (rt,raw,observed,lat,lon):
                continue
            v=feed.entity.add(id=str(i)).vehicle
            v.vehicle.id,v.timestamp='sample',int(observed)
            v.trip.route_id,v.trip.trip_id,v.trip.start_date=str(int(rt)),str(raw),when.strftime('%Y%m%d')
            v.position.latitude,v.position.longitude=float(lat),float(lon)
        live.apply_positions(feed,received)
    with patch.object(app.state,'planner',SimpleNamespace(live=live,router=router),create=True):
        result=arrivals(target,route,when.isoformat(),None)
    return PlatformResponse.model_validate(result).model_dump(mode='json',exclude_none=True)


def run(directory):
    model=EtaModel()
    router=Router(ROOT/'delhi_buses_static_gtfs_v1.zip',ROOT/'data',None,model)
    registry=CapturedOverlay(model,ROOT/'data',0).bind(router)
    rows=[]
    with baseline_package() as old:
        old_class=importlib.import_module(old+'.replay').CapturedOverlay
        for day in DAYS:
            with open_db(directory/('bus_movements_'+day.replace('-','_')+'.db')) as db:
                for window in WINDOWS:
                    found=candidate(db,day,window,router,registry)
                    if not found:
                        rows.append({'day':day,'window':window,'eligible':False})
                        continue
                    vehicle,raw,route,stamp,when,lat,lon,origin,target=found
                    issue=int(when.timestamp())
                    feed=protobuf().FeedMessage()
                    feed.header.gtfs_realtime_version,feed.header.timestamp='2.0',issue
                    v=feed.entity.add(id='sample').vehicle
                    v.vehicle.id,v.timestamp='sample',stamp
                    v.trip.trip_id,v.trip.route_id,v.trip.start_date=raw,route,day.replace('-','')
                    v.position.latitude,v.position.longitude=lat,lon
                    row={'day':day,'window':window,'eligible':True,'issue':when.isoformat(),'gps_age_seconds':issue-stamp,
                         'route_id':route,'route':router.routes[route][0],'observed_stop':origin,'target':target,
                         'target_name':router.stops[target]['name'],'provider_format':'numeric_suffix' if raw.count('_')==3 else 'bare'}
                    for label,kind in [('previous',old_class),('revised',CapturedOverlay)]:
                        overlay=kind(model,ROOT/'data',issue)
                        for key in ('router','ids','by_clock','edges'):
                            setattr(overlay,key,getattr(registry,key))
                        overlay.apply_positions(feed,issue)
                        response=platform_estimate(router,overlay,target,route,when)
                        anchors=overlay.recent_passages(when)
                        assert len(anchors)==1 and anchors[0]['stop_id']==origin
                        row[label]={'can_use_position':overlay.can_approach(anchors[0],target,when),
                                    'source':response['source'],'wait_seconds':response['wait_seconds']}
                    row['later_gps'] = arrival_bracket(db,router,vehicle,raw,route,stamp,when,(lat,lon),target)
                    row['anonymous_path_check'] = agreement(router,route,origin,target,when)
                    row['endpoint_with_causal_history'] = archived_endpoint(db,router,registry,vehicle,when,route,target)
                    rows.append(row)
    return {'protocol':'unresolved-clock-arrivals-v1','examples':rows}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--existing',type=Path,default=ROOT/'.near-miss-peak-20261121')
    p.add_argument('--output',type=Path,default=ROOT/'data/fleet-arrivals-fallback.json')
    args=p.parse_args()
    result=run(args.existing)
    temp=args.output.with_suffix('.pending')
    temp.write_text(json.dumps(result,indent=2),encoding='utf-8')
    temp.replace(args.output)
    print(json.dumps(result))
