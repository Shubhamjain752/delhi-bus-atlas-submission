"""Replay the locked route-observation geometry and audit next-stop times for unmatched clocks."""
import argparse
from collections import Counter,defaultdict
from contextlib import closing
from datetime import date,datetime,time,timedelta
from functools import lru_cache
import gzip
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import statistics
import sys
import tempfile
import time as clock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from transit.arrivals import platform_estimate
from transit.eta import EtaModel,read_data
from transit.live import protobuf
from transit.replay import CapturedOverlay
from transit.route_observation import directed_observation,plausible_service_days,route_observation,unanimous_paths
from transit.router import IST,Pattern,Router,approximate_m,coords
from transit.schedule import cache_schedule

DEV=[f'2026-10-{n:02}' for n in range(1,24)]
CONFIRM=[f'2026-10-{n:02}' for n in range(24,32)]
MONTH=DEV+CONFIRM
PROTOCOL='unassigned-clock-observation-confirmation-v1'


def save(path,value,compressed=False):
    part=path.with_suffix('.pending')
    with part.open('wb') as stream:
        raw=json.dumps(value,separators=(',',':'),allow_nan=False).encode()
        if compressed:
            with gzip.GzipFile(fileobj=stream,filename='',mode='wb',mtime=0) as gz:
                gz.write(raw)
        else:
            stream.write(raw)
    part.replace(path)


def cases(path):
    """Stream the case array in the extractor's compact JSON without loading a city's day at once."""
    marker='"cases":['
    with gzip.open(path,'rt',encoding='utf-8') as stream:
        buffer=''
        while marker not in buffer:
            part=stream.read(65536)
            if not part:
                raise ValueError('Missing cases array')
            buffer=buffer[-len(marker):]+part
        buffer=buffer.partition(marker)[2]
        decoder=json.JSONDecoder()
        while True:
            buffer=buffer.lstrip(' \t\r\n,')
            if buffer.startswith(']'):
                return
            try:
                value,end=decoder.raw_decode(buffer)
            except json.JSONDecodeError:
                part=stream.read(65536)
                if not part:
                    raise ValueError('Truncated GPS cases')
                buffer+=part
                continue
            yield value
            buffer=buffer[end:]


class ReplayRouter(Router):
    """Reuse the production stop-to-shape assignments across dates of the same static feed."""
    @lru_cache(maxsize=500)
    def _geometry(self,pid):
        pattern=self.patterns[pid]
        key=(pattern.shape,tuple(pattern.stops))
        anchors=self.geometry_anchors.get(key)
        if anchors is None:
            points,indexes,kind=super()._geometry(pid)
            self.geometry_anchors[key]=indexes,kind
            return points,indexes,kind
        indexes,kind=anchors
        if kind=='ordered_stops':
            points=[list(coords(self.stops[s])) for s in pattern.stops]
        else:
            with closing(sqlite3.connect(self.cache_path)) as db:
                raw=db.execute('select points from shapes where id=?',(pattern.shape,)).fetchone()[0]
            points=json.loads(raw)
        return points,indexes,kind


class Audit:
    def __init__(self):
        self.model=EtaModel(read_data(ROOT/'data/eta_model.json.gz'))
        self.model.weekend=EtaModel(read_data(ROOT/'data/weekend/eta_model.json.gz'))
        self.path=cache_schedule(ROOT/'delhi_buses_static_gtfs_v1.zip',ROOT/'data')
        self.published=set()
        self.pattern_ids=defaultdict(list)
        self.geometry_anchors={}
        self.max_service=0
        with closing(sqlite3.connect(self.path)) as db:
            meta={k:json.loads(v) for k,v in db.execute('select * from metadata')}
            for pid,r,starts,offsets in db.execute('select rowid,route,trips,departures from patterns'):
                self.pattern_ids[r].append(pid)
                t=json.loads(starts)
                d=json.loads(offsets)
                self.max_service=max(self.max_service,t[-1][0]+d[-1])
                self.published.update(int(r)*10000+(s+d[0])//60 for s,_ in t)
        self.stops={sid:dict(id=sid,name=n,lat=y,lon=x) for sid,n,y,x in meta['stops']}
        self.names={sid:(n,agency) for sid,n,agency in meta['routes']}
        self.calendar=meta['calendar']
        self.exceptions={(r['date'],r['service_id']):r['exception_type'] for r in meta['exceptions']}

    def load_route(self,route):
        # All path and platform queries are route-specific. Releasing the last
        # route avoids paging the entire city's stop-time objects while the raw scan runs.
        Router.platform_path.cache_clear()
        Router._geometry.cache_clear()
        ReplayRouter._geometry.cache_clear()
        self.paths.cache_clear()
        network=ReplayRouter.__new__(ReplayRouter)
        network.geometry_anchors=self.geometry_anchors
        network.stops,network.cache_path=self.stops,self.path
        network.routes=self.names
        network.calendar,network.exceptions=self.calendar,self.exceptions
        network.eta,network.walking=self.model,None
        network.max_service=self.max_service
        network.at,network.patterns=defaultdict(list),[]
        with closing(sqlite3.connect(self.path)) as db:
            for dbid in self.pattern_ids[route]:
                r,service,shape,*values=db.execute('select * from patterns where rowid=?',(dbid,)).fetchone()
                stops,arr,dep,pick,drop,seq,runs=map(json.loads,values)
                starts,ids=zip(*runs)
                pid=len(network.patterns)
                network.patterns.append(Pattern(r,service,shape,stops,arr,dep,pick,drop,seq,starts,ids))
                for i,s in enumerate(stops):
                    network.at[s].append((pid,i))
        self.router=network
        network.grid=defaultdict(list)
        for s in network.at:
            network.grid[network._cell(coords(network.stops[s]))].append(s)
        self.registry=CapturedOverlay(self.model,ROOT/'data',0).bind(network)

    @lru_cache(maxsize=512)
    def active(self,day):
        return self.router._active(day)

    def plausible_dates(self,when):
        return plausible_service_days(self.router,when)

    @lru_cache(maxsize=30000)
    def paths(self,route,origin,target,days):
        return unanimous_paths(self.router,route,origin,target,days)

    @staticmethod
    def causal_pair(row):
        issue=row['forecast_receipt']
        points=[]
        for receipt,stamp,lat,lon in row['observations']:
            if receipt>issue:
                break
            if not issue-180<=stamp<=receipt:
                continue
            if points and stamp<points[-1][0]:
                continue
            point=(lat,lon)
            if points and stamp==points[-1][0]:
                if point!=points[-1][2]:
                    return None
                continue
            points.append((stamp,receipt,point))
        if len(points)<2 or issue-points[-1][0]>60 or points[-1][0]!=row['origin_gps']:
            return None
        a,b=points[-2:]
        if b[0]-a[0]>60 or approximate_m(a[2],b[2])>35*(b[0]-a[0]):
            return None
        return a,b

    def real_response(self,row,pair,when):
        issue=int(when.timestamp())
        overlay=CapturedOverlay(self.model,ROOT/'data',issue)
        for field in ('router','ids','edges','by_clock'):
            setattr(overlay,field,getattr(self.registry,field))
        route,minute=row['route_id'],row['start_minute']
        # The archive has no original TripDescriptor.start_date. Its retained physical
        # and run keys are opaque. Encode a supported, unmatched clock and assign the
        # receipt's civil date; the path gate still tests every plausible active calendar.
        raw=f'{route}_{minute//60:02}_{minute%60:02}_0'
        for stamp,receipt,point in pair:
            feed=protobuf().FeedMessage()
            feed.header.gtfs_realtime_version,feed.header.timestamp='2.0',receipt
            v=feed.entity.add(id='archived').vehicle
            v.timestamp,v.vehicle.id=stamp,row['bus']
            v.trip.trip_id,v.trip.route_id,v.trip.start_date=raw,route,when.strftime('%Y%m%d')
            v.position.latitude,v.position.longitude=point
            overlay.apply_positions(feed,receipt)
        ordinary=platform_estimate(self.router,overlay,row['stop'],route,when)
        observation=route_observation(self.router,overlay,row['stop'],route,when,ordinary)
        return ordinary['wait_seconds'],observation

    def evaluate(self,row):
        issue,route,origin,target = row['forecast_receipt'],row['route_id'],row['upstream'],row['stop']
        when=datetime.fromtimestamp(issue,IST)
        days=self.plausible_dates(when)
        paths=self.paths(route,origin,target,days)
        if not paths:
            active=set.union(*(self.active(d) for d in days))
            if any(self.router.patterns[pid].service in active and self.router.patterns[pid].stops[-1]==target
                   for pid,_ in self.router.at[origin]):
                return 'target_terminal_on_an_active_pattern',None
            return 'incompatible_boarding_path',None
        pair=self.causal_pair(row)
        if not pair:
            return 'no_two_recent_issue_time_fixes',None
        if not directed_observation(self.router,paths,origin,pair,issue):
            return 'direction_or_platform_not_confirmed',None
        # All paths agreed. The actual service day is unavailable. Use a historical clock
        # only when every plausible calendar produces the same running-time estimate.
        pid,i,j=max(paths,key=lambda item:len(self.router.patterns[item[0]].starts))
        pattern=self.router.patterns[pid]
        scheduled=pattern.arrivals[j]-pattern.arrivals[i]
        estimates=[]
        for day in days:
            if pattern.service not in self.active(day):
                continue
            midnight=int(datetime.combine(day,time(),IST).timestamp())
            estimates.append(self.model.duration(route,origin,target,row['origin_gps']-midnight,scheduled,
                                                 asof=issue,service_date=day)[0])
        duration=estimates[0] if len(set(estimates))==1 else scheduled
        wait=row['origin_gps']+duration-issue
        strict,observation=self.real_response(row,pair,when)
        if observation is None:
            return 'production_observation_withheld',None
        if observation['observed_stop']['id']!=origin or observation['trip_assignment']!='unmatched_start_clock':
            raise ValueError('Production observation does not describe selected upstream passage')
        answer={'day':row['day'],'route':route,'stop':target,'from_stop':origin,'issue':issue,
                'gps_age_s':issue-pair[1][0],'wait_s':wait,'ordinary_wait_s':strict,
                'timetable_link_wait_s':row['origin_gps']+scheduled-issue}
        brackets,receipts=row['gps_entry_exit'],row['gps_entry_exit_receipts']
        if brackets and receipts and brackets[0] is not None and brackets[1] is not None and receipts[1]>issue:
            answer.update(lower_s=brackets[0]-issue,upper_s=brackets[1]-issue)
            if brackets[2] is not None:
                answer['exit_s']=brackets[2]-issue
        return 'movement_hint',answer


def summarize(rows):
    verified=[r for r in rows if 'upper_s' in r]
    result={'hints':len(rows),'verified':len(verified),'unverified':len(rows)-len(verified),
            'ordinary_eta_unavailable':sum(r['ordinary_wait_s'] is None for r in rows),
            'ordinary_later_by_at_least_5_min':sum(r['ordinary_wait_s'] is not None and r['ordinary_wait_s']>=r['wait_s']+300
                                                   for r in rows)}
    if verified:
        for k,name in [('wait_s','estimate'),('timetable_link_wait_s','scheduled_link')]:
            errors=[r[k]-(r['upper_s']+r['lower_s'])/2 for r in verified]
            absolute=sorted(map(abs,errors))
            result[name]={'within_60s':sum(x<=60 for x in absolute),'within_120s':sum(x<=120 for x in absolute),
                          'median_absolute_s':statistics.median(absolute),'p90_absolute_s':absolute[(9*len(absolute)-1)//10],
                          'over_120s_early':sum(x< -120 for x in errors),'over_120s_late':sum(x>120 for x in errors),
                          'within_60s_of_bracket':sum(r['lower_s']-60<=r[k]<=r['upper_s']+60 for r in verified)}
        result['entered_by_issue']=sum(r['upper_s']<=0 for r in verified)
        result['exited_by_issue']=sum(r.get('exit_s',1)<=0 for r in verified)
        result['confirmed_still_upstream_at_issue']=sum(r['lower_s']>=0 for r in verified)
        result['median_arrival_bracket_s']=statistics.median(r['upper_s']-r['lower_s'] for r in verified)
    return result


def aggregate(staged,days,digest,policy):
    coverage=Counter()
    rows=[]
    for day in days:
        coverage.update(staged[day]['coverage'])
        rows.extend(staged[day]['rows'])
    per_route=defaultdict(list)
    for r in rows:
        per_route[r['route']].append(r)
    return {'protocol':PROTOCOL,'days':days,'script_sha256':digest,'policy_sha256':policy,
            'coverage':dict(coverage),'total':summarize(rows),'route_ids_with_hint':len(per_route),
            'overnight':summarize([r for r in rows if datetime.fromtimestamp(r['issue'],IST).hour<6]),
            'by_hour':{str(h):summarize([r for r in rows if datetime.fromtimestamp(r['issue'],IST).hour==h]) for h in range(24)},
            'by_route':{k:summarize(v) for k,v in sorted(per_route.items())}}


def run(case_dir,out,days,wait,background=True):
    if os.name=='nt' and background:
        import ctypes as ct
        k=ct.windll.kernel32
        k.GetCurrentProcess.restype=ct.c_void_p
        k.SetPriorityClass.argtypes=[ct.c_void_p,ct.c_uint32]
        k.SetPriorityClass(k.GetCurrentProcess(),0x4000)
    audit=Audit()
    out.mkdir(parents=True,exist_ok=True)
    digest=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    policy=hashlib.sha256((ROOT/'transit/route_observation.py').read_bytes()).hexdigest()
    staged={}
    pending=set(days)
    while pending:
        for day in sorted(pending.copy()):
            target=out/(day+'.json.gz')
            source=case_dir/(day+'.json.gz')
            if not source.exists():
                continue
            payload=None
            source_hash=hashlib.sha256(source.read_bytes()).hexdigest()
            if target.exists():
                with gzip.open(target,'rt') as stream:
                    payload=json.load(stream)
                if payload['script_sha256']!=digest or payload.get('policy_sha256')!=policy or payload['source_sha256']!=source_hash:
                    payload=None
            if payload is None:
                counts=Counter()
                rows=[]
                with tempfile.TemporaryDirectory(prefix='delhi-anon-sort-') as temp, closing(
                        sqlite3.connect(Path(temp)/'cases.sqlite')) as db:
                    db.execute('create table cases (route text, data text)')
                    for row in cases(source):
                        counts['route_hour_candidates']+=1
                        minute=row.get('start_minute')
                        if minute is None or int(row['route_id'])*10000+minute in audit.published:
                            continue
                        counts['unresolved_clock']+=1
                        db.execute('insert into cases values (?,?)',(row['route_id'],json.dumps(row,separators=(',',':'))))
                    print(day,'screening',counts['unresolved_clock'],'unresolved clocks',flush=True)
                    route=None
                    for index,(r,raw) in enumerate(db.execute('select route,data from cases order by route'),1):
                        if r!=route:
                            audit.load_route(r)
                            route=r
                        reason,hint=audit.evaluate(json.loads(raw))
                        counts[reason]+=1
                        if hint:
                            rows.append(hint)
                        if index%2000==0:
                            print(day,'processed',index,'of',counts['unresolved_clock'],flush=True)
                payload={'day':day,'protocol':PROTOCOL,'script_sha256':digest,'policy_sha256':policy,'source_sha256':source_hash,
                         'coverage':dict(counts),'rows':rows}
                save(target,payload,compressed=True)
            staged[day]=payload
            pending.remove(day)
            print(day,payload['coverage'].get('unresolved_clock',0),summarize(payload['rows']),flush=True)
            for split,subset in [('development',DEV),('confirmation',CONFIRM)]:
                if day in subset and set(subset)<=staged.keys():
                    save(out/(split+'.json'),aggregate(staged,subset,digest,policy))
        if pending:
            if not wait:
                raise ValueError('Missing selected development days: '+str(sorted(pending)))
            clock.sleep(20)
    if hashlib.sha256((ROOT/'transit/route_observation.py').read_bytes()).hexdigest()!=policy:
        raise ValueError('Observation policy changed during replay')
    save(out/'report.json',aggregate(staged,days,digest,policy))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input',type=Path,default=ROOT/'data/fleet-cases')
    p.add_argument('--output',type=Path,default=ROOT/'data/fleet-anonymous-arrivals')
    p.add_argument('--days',nargs='+',choices=MONTH,default=MONTH)
    p.add_argument('--wait',action='store_true')
    p.add_argument('--normal-priority',action='store_true',help='Use the normal Windows scheduler after the archive scan finishes.')
    a=p.parse_args()
    if a.days!=sorted(set(a.days)):
        p.error('Days must be chronological and unique.')
    run(a.input,a.output,a.days,a.wait,background=not a.normal_priority)
