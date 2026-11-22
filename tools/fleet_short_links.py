"""Secondary October study for route IDs with only two or three scheduled stops.

A forecast is issued only on a corroborated departure from the origin. Terminal
arrival error and intermediate boarding proxies remain separate from journey offers.
"""
import argparse
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import lru_cache
import hashlib
import gzip
import json
import math
import os
from pathlib import Path
import sqlite3
import statistics
import sys
import time as clock

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from transit.eta import EtaModel, read_data
from transit.router import IST, Pattern, Router, approximate_m, coords
from transit.schedule import cache_schedule

MONTH = [f'2026-10-{n:02}' for n in range(1,32)]


class ShortRouter(Router):
    """Read only the required published patterns into memory; retain production shape matching."""
    def __init__(self):
        self.cache_path = cache_schedule(ROOT/'delhi_buses_static_gtfs_v1.zip',ROOT/'data')
        self.patterns = []
        with closing(sqlite3.connect(self.cache_path)) as db:
            meta = {k:json.loads(v) for k,v in db.execute('SELECT * FROM metadata')}
            query = ('SELECT * FROM patterns WHERE route IN (SELECT route FROM patterns GROUP BY route '
                     'HAVING MAX(json_array_length(stops)) IN (2,3))')
            for route,service,shape,*values in db.execute(query):
                stops,arr,dep,pick,drop,seq,trips = map(json.loads,values)
                starts,ids = zip(*trips)
                self.patterns.append(Pattern(route,service,shape,stops,arr,dep,pick,drop,seq,starts,ids))
        self.stops = {s:dict(id=s,name=n,lat=y,lon=x) for s,n,y,x in meta['stops']}
        self.routes = {s:(n,agency) for s,n,agency in meta['routes']}
        self.calendar = meta['calendar']
        self.exceptions = {(r['date'],r['service_id']):r['exception_type'] for r in meta['exceptions']}


def save(path,data,compressed=False):
    part = path.with_suffix(path.suffix+'.pending')
    raw = json.dumps(data,separators=(',',':'),allow_nan=False).encode()
    with part.open('wb') as dest:
        if compressed:
            with gzip.GzipFile(fileobj=dest,mode='wb',filename='',mtime=0) as gz:
                gz.write(raw)
        else:
            dest.write(raw)
    part.replace(path)


@lru_cache(maxsize=500)
def phase(stop):
    return int(hashlib.blake2s((stop+'|platform-grid').encode(),digest_size=8).hexdigest(),16)%120


class BackgroundIo:
    """Yield to the main replay when Windows is actively paging under memory pressure."""
    def __init__(self):
        self.next_check = 0
        self.counter = None
        if os.name!='nt':
            return
        import ctypes as ct
        self.ct = ct
        self.kernel = ct.windll.kernel32
        self.kernel.GetCurrentProcess.restype = ct.c_void_p
        self.kernel.SetPriorityClass.argtypes = [ct.c_void_p,ct.c_uint32]
        self.kernel.SetPriorityClass(self.kernel.GetCurrentProcess(),0x00100000)
        self.pdh = ct.windll.pdh
        query,counter = ct.c_void_p(),ct.c_void_p()
        if not self.pdh.PdhOpenQueryW(None,0,ct.byref(query)):
            if not self.pdh.PdhAddEnglishCounterW(query,r'\Memory\Pages Input/sec',0,ct.byref(counter)):
                self.counter = counter
                self.pdh.PdhCollectQueryData(query)
            self.query = query

    def throttle(self):
        if not self.counter or clock.monotonic()<self.next_check:
            return
        ct = self.ct
        self.next_check = clock.monotonic()+3
        memory = (ct.c_uint64*8)()
        memory[0] = ct.sizeof(memory)
        value = (ct.c_double*2)()
        if (self.kernel.GlobalMemoryStatusEx(ct.byref(memory)) and not self.pdh.PdhCollectQueryData(self.query)
                and not self.pdh.PdhGetFormattedCounterValue(self.counter,0x200,None,ct.byref(value))):
            available = memory[2]
            if available<256*1024**2 or available<1536*1024**2 and value[1]>1500:
                clock.sleep(3)


def chunks(path):
    # Reading each 3 MiB chunk releases it after filtering, even on a memory-constrained host.
    with path.open('rb',buffering=0) as stream:
        version = np.lib.format.read_magic(stream)
        if version==(1,0):
            shape,fortran,dtype = np.lib.format.read_array_header_1_0(stream)
        elif version==(2,0):
            shape,fortran,dtype = np.lib.format.read_array_header_2_0(stream)
        else:
            raise ValueError('Unsupported GPS shard format')
        if len(shape)!=1 or fortran or dtype.hasobject:
            raise ValueError('Unexpected GPS shard shape')
        for first in range(0,shape[0],65536):
            required = min(65536,shape[0]-first)
            chunk = np.fromfile(stream,dtype=dtype,count=required)
            if len(chunk)!=required:
                raise ValueError('Short GPS shard read')
            yield chunk


@dataclass(frozen=True)
class Link:
    pid: int
    stops: tuple
    locations: tuple
    scheduled: int
    intermediate_boardable: bool
    reference: str


class Catalogue:
    def __init__(self,router):
        self.router = router
        by_route = defaultdict(list)
        by_clock = defaultdict(list)
        for pid,p in enumerate(router.patterns):
            by_route[int(p.route)].append(pid)
        self.routes = {r:max(len(router.patterns[pid].stops) for pid in ids) for r,ids in by_route.items()
                       if max(len(router.patterns[pid].stops) for pid in ids) in (2,3)}
        self.by_route = {r:ids for r,ids in by_route.items() if r in self.routes}
        for r,ids in self.by_route.items():
            for pid in ids:
                p = router.patterns[pid]
                for k,start in enumerate(p.starts):
                    by_clock[r,(start+p.departures[0])//60].append((pid,k))
        self.by_clock = by_clock

    @lru_cache(maxsize=30000)
    def link(self,route,minute):
        exact = self.by_clock.get((route,minute),())
        ids = [x[0] for x in exact] if exact else self.by_route[route]
        variants = {tuple(self.router.patterns[i].stops) for i in ids}
        if len(variants)!=1:
            return None
        stops, = variants
        if len(stops)<2 or len(set(stops))!=len(stops):
            return None
        locations = tuple(coords(self.router.stops[s]) for s in stops)
        if approximate_m(*locations[:2])<100:
            return None
        pid = max(ids,key=lambda i:len(self.router.patterns[i].starts))
        schedules = [self.router.patterns[i].arrivals[1]-self.router.patterns[i].departures[0] for i in ids]
        if any(x<=0 for x in schedules):
            return None
        boardable = len(stops)==3 and approximate_m(*locations[1:])>=100 and all(
            not self.router.patterns[i].pickups[1] and not self.router.patterns[i].drops[2] for i in ids)
        return Link(pid,stops,locations,round(statistics.median(schedules)),boardable,
                    'provider_clock' if exact else 'unique_route_sequence')

    @lru_cache(maxsize=20000)
    def days(self,day):
        return [(d,self.router._active(d),int(datetime.combine(d,time(),IST).timestamp()))
                for d in (day-timedelta(days=1),day,day+timedelta(days=1))]

    def service_day(self,route,minute,link,at):
        day = datetime.fromtimestamp(at,IST).date()
        # Unknown clocks allow route-level timing only. Never invent an operating date.
        dates = {d for d,active,midnight in self.days(day)
                 for pid,k in self.by_clock.get((route,minute),())
                 for p in [self.router.patterns[pid]]
                 if p.service in active and abs(midnight+p.starts[k]+p.departures[0]-at)<=5400}
        return next(iter(dates)) if len(dates)==1 else None

    def forward(self,link,i,previous,location):
        a,b = link.locations[i:i+2]
        path = self.router.platform_path(link.pid,i)
        if path is not None:
            measure = path.project(location)
            prior = path.project(previous)
            return measure is not None and prior is not None and measure>=20 and measure-prior>=20
        scale = 111195*math.cos(math.radians((a[0]+b[0])/2))
        dx,dy = (b[1]-a[1])*scale,(b[0]-a[0])*111195
        x,y = (location[1]-a[1])*scale,(location[0]-a[0])*111195
        length = math.hypot(dx,dy)
        prior_x,prior_y = (previous[1]-a[1])*scale,(previous[0]-a[0])*111195
        along = (dx*x+dy*y)/length
        prior = (dx*prior_x+dy*prior_y)/length
        return 20<=along<=length+45 and along-prior>=20 and abs(x*dy-y*dx)/length<=75


def at_stop(location,link):
    distances = sorted((approximate_m(location,point),i) for i,point in enumerate(link.locations))
    return distances[0][1] if distances[0][0]<=45 and distances[1][0]-distances[0][0]>=12 else -1


class TerminalReplay:
    def __init__(self,catalogue,model):
        self.catalogue,self.model = catalogue,model
        self.tracks,self.events = {},[]
        self.counts = defaultdict(Counter)

    def finish(self,track,reason):
        event = track.pop('event',None)
        if event is None:
            return
        event['end_reason'] = reason
        if event['near_ready'] is not None:
            if 'entry_lower' not in event:
                event['boarding_label'] = 'unverified'
            elif event['near_ready'] <= event['entry_lower']:
                event['boarding_label'] = 'approaching'
            elif event.get('target_exit') is not None and event['near_ready']>=event['target_exit']:
                event['boarding_label'] = 'gone'
            else:
                event['boarding_label'] = 'uncertain'
        self.events.append(event)

    def issue(self,r,m,link,receipt,obs,previous):
        # GPS brackets the actual departure; use its midpoint. Both fixes are already available.
        departure = (previous+obs)//2
        operating_day = self.catalogue.service_day(r,m,link,departure)
        duration,source = link.scheduled,'timetable'
        if operating_day is not None:
            midnight = int(datetime.combine(operating_day,time(),IST).timestamp())
            duration,source = self.model.duration(str(r),*link.stops[:2],departure-midnight,link.scheduled,
                                                  asof=receipt,service_date=operating_day)
        predicted = departure+int(duration)
        offset = phase(link.stops[1])
        ready = ((predicted-offset+60)//120)*120+offset
        near = ready if link.intermediate_boardable and 1<=ready-predicted<=30 and receipt<ready else None
        return dict(route=r,stops=len(link.stops),from_stop=link.stops[0],to_stop=link.stops[1],
                    issue=receipt,departure_lower=previous,departure_upper=obs,forecast=predicted,
                    schedule_forecast=departure+link.scheduled,reference=link.reference,
                    service_day=operating_day.isoformat() if operating_day else None,
                    eta_source=source,near_ready=near)

    def accept(self,r,bus,trip,minute,obs,receipt,location):
        track = self.tracks.get(bus)
        key = (r,trip)
        if track and track['key']==key and obs<=track['obs']:
            if obs==track['obs'] and track['location']!=location:
                self.finish(track,'conflicting_timestamp')
                del self.tracks[bus]
            return
        if track and (track['key']!=key or obs<=track['obs'] or obs-track['obs']>60 or
                      approximate_m(track['location'],location)>35*(obs-track['obs'])):
            self.finish(track,'identity_or_continuity_lost')
            track = None
        if not track:
            link = self.catalogue.link(r,minute)
            if link is None:
                self.tracks.pop(bus,None)
                return
            track = self.tracks[bus] = dict(key=key,link=link,at=-1,origin_since=None)
        link = track['link']
        at = at_stop(location,link)
        previous,old_at = track.get('obs'),track['at']
        event = track.get('event')
        if event:
            if at==1 and 'entry_lower' not in event:
                if receipt<=event['issue']:
                    self.finish(track,'target_already_reported_at_issue')
                elif old_at!=1 and approximate_m(track['location'],link.locations[1])>45:
                    event.update(entry_lower=previous,entry_upper=obs,entry_received=receipt)
                    if event['near_ready'] is None or event['near_ready']<=previous:
                        self.finish(track,'target_entry_observed')
            elif 'entry_lower' in event and old_at==1 and at!=1:
                if self.catalogue.forward(link,1,track['location'],location):
                    event['target_exit'] = obs
                self.finish(track,'target_exit_observed' if 'target_exit' in event else 'exit_direction_unconfirmed')
            elif at==0 and old_at!=0:
                self.finish(track,'returned_to_origin')
            elif at==2 and 'entry_lower' not in event:
                self.finish(track,'intermediate_entry_unobserved')
        if at==0:
            if old_at!=0:
                track['origin_since'] = obs
        elif old_at==0:
            since = track['origin_since']
            if since is not None and previous-since>=10:
                self.counts[r]['departures_after_observed_origin_dwell']+=1
                if at==1:
                    self.counts[r]['target_in_exit_receipt']+=1
                elif at==2:
                    self.counts[r]['already_at_third_stop']+=1
                elif approximate_m(location,link.locations[0])>45 and self.catalogue.forward(link,0,track['location'],location):
                    track['event'] = self.issue(r,minute,link,receipt,obs,previous)
                else:
                    self.counts[r]['forward_exit_unconfirmed']+=1
            track['origin_since'] = None
        track.update(at=at,obs=obs,location=location)

    def expire(self,earliest_possible_obs):
        for bus,track in list(self.tracks.items()):
            if track['obs']+60 < earliest_possible_obs:
                self.finish(track,'gps_coverage_ended')
                del self.tracks[bus]


def error_summary(events,key):
    confirmed = [e for e in events if 'entry_lower' in e]
    if not confirmed:
        return {'verified_entries':0}
    errors = [abs(e[key]-(e['entry_lower']+e['entry_upper'])/2) for e in confirmed]
    distances = [max(0,e['entry_lower']-e[key],e[key]-e['entry_upper']) for e in confirmed]
    return {'verified_entries':len(errors),'median_absolute_midpoint_error_s':statistics.median(errors),
            'within_60s_of_midpoint':sum(x<=60 for x in errors),
            'within_60s_of_bracket':sum(x<=60 for x in distances),
            'median_entry_bracket_s':statistics.median(e['entry_upper']-e['entry_lower'] for e in confirmed)}


def group(events):
    return {'issued':len(events),'model':error_summary(events,'forecast'),
            'timetable':error_summary(events,'schedule_forecast'),
            'endings':dict(Counter(e['end_reason'] for e in events)),
            'boarding':dict(Counter(e['boarding_label'] for e in events if 'boarding_label' in e)),
            'eta_sources':dict(Counter(e['eta_source'] for e in events)),
            'past_prediction_at_issue':sum(e['forecast']<e['issue'] for e in events),
            'matched_service_day':sum(e['service_day'] is not None for e in events)}


def report(replay,counts,days):
    events = replay.events
    cat = replay.catalogue
    by_route = defaultdict(list)
    for e in events:
        by_route[e['route']].append(e)
    by_hour = defaultdict(list)
    for e in events:
        by_hour[datetime.fromtimestamp(e['issue'],IST).hour].append(e)
    desc = {}
    for r,n in sorted(cat.routes.items()):
        raw,valid = counts[r]
        desc[str(r)] = {'name':cat.router.routes[str(r)][0],'maximum_stops':n,'raw':raw,'fresh':valid,
                        **dict(replay.counts[r]),**group(by_route[r])}
    return {'protocol':'short-terminal-link-v1','days':days,'complete':days==MONTH,
            'input_archive_sha256':(ROOT/'SHA256SUMS.txt').read_text().split()[0],
            'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'total':group(events),'two_stop_links':group([e for e in events if e['stops']==2]),
            'three_stop_links':group([e for e in events if e['stops']==3]),
            'overnight_00_06':group([e for e in events if datetime.fromtimestamp(e['issue'],IST).hour<6]),
            'holdout_24_31':group([e for e in events if datetime.fromtimestamp(e['issue'],IST).day>=24]),
            'by_issue_hour_ist':{str(h):group(by_hour[h]) for h in range(24)},
            'routes':desc}


def stage(directory,day,routes,mask,io):
    marker = json.loads((directory/f'{day}.json').read_text())
    hours = marker['hours']
    if len(hours)!=24:
        raise ValueError('Expected 24 receipt hours on '+day)
    per_hour, counts = [],defaultdict(lambda:[0,0])
    for hour in hours:
        size = 0
        for route,(raw,valid) in hour.items():
            if route.isdigit() and int(route) in routes:
                total = counts[int(route)]
                total[0]+=raw
                total[1]+=valid
                size+=valid
        per_hour.append(size)
    # Persist only these sparse rows in the existing private shard workspace.
    # A development smoke test and the main chronological replay share the same copy.
    stamp = hashlib.sha256((marker['crc32']+str(sorted(routes))+str(per_hour)).encode()).hexdigest()[:16]
    folder = directory/'short-route-subset'
    folder.mkdir(exist_ok=True)
    final = folder/f'{day}-{stamp}.npy'
    if not sum(per_hour) or final.exists():
        return final,counts
    part = final.with_suffix('.pending')
    dest,cursor = None,0
    for h,expected in enumerate(per_hour):
        if not expected:
            continue
        found = 0
        for chunk in chunks(directory/f'{day}-{h:02}.npy'):
            io.throttle()
            if dest is None:
                dest = np.lib.format.open_memmap(part,mode='w+',dtype=chunk.dtype,shape=(sum(per_hour),))
            short = chunk[mask[chunk['route']]]
            found+=len(short)
            dest[cursor:cursor+len(short)] = short
            cursor+=len(short)
        if found!=expected:
            raise ValueError('Short-route subset differs from source route/hour census')
    if cursor!=sum(per_hour):
        raise ValueError('Incomplete short-route subset')
    dest.flush()
    del dest
    part.replace(final)
    return final,counts


def run(directory,days,output,wait):
    io = BackgroundIo()
    script_digest = hashlib.sha256(Path(__file__).read_bytes()).digest()
    cat = Catalogue(ShortRouter())
    model = EtaModel(read_data(ROOT/'data/eta_model.json.gz'))
    model.weekend = EtaModel(read_data(ROOT/'data/weekend/eta_model.json.gz'))
    replay = TerminalReplay(cat,model)
    mask = np.zeros(65536,'?')
    mask[list(cat.routes)] = True
    counts = defaultdict(lambda:[0,0])
    remaining,staged,index = set(days),{},0
    while index<len(days):
        for day in sorted(remaining.copy()):
            if (directory/f'{day}.json').is_file():
                staged[day] = stage(directory,day,cat.routes,mask,io)
                remaining.remove(day)
                print(day,'short route subset staged',flush=True)
        while index<len(days) and days[index] in staged:
            day = days[index]
            path,daily = staged.pop(day)
            for r,(raw,valid) in daily.items():
                counts[r][0]+=raw
                counts[r][1]+=valid
            size = sum(v[1] for v in daily.values())
            found = 0
            if size:
                for short in chunks(path):
                    for f in short:
                        replay.accept(int(f['route']),int(f['bus']),int(f['trip']),int(f['minute']),
                                      int(f['obs']),int(f['recv']),(float(f['lat']),float(f['lon'])))
                    found += len(short)
            if found!=size:
                raise ValueError('Cached daily subset differs from source count')
            end = int(datetime.combine(date.fromisoformat(day)+timedelta(days=1),time(),IST).timestamp())
            replay.expire(end-90)
            print(day,'short links',len(replay.events),'resolved or censored',flush=True)
            index+=1
        if index<len(days):
            if not wait:
                raise ValueError('Complete source hour shards are unavailable for '+days[index])
            clock.sleep(15)
    for track in replay.tracks.values():
        replay.finish(track,'archive_coverage_ended')
    if hashlib.sha256(Path(__file__).read_bytes()).digest()!=script_digest:
        raise ValueError('Study source changed while scanning')
    save(output,report(replay,counts,days))
    save(output.with_name(output.stem+'-events.json.gz'),replay.events,compressed=True)


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scratch',type=Path,required=True)
    parser.add_argument('--days',nargs='+',choices=MONTH,default=MONTH)
    parser.add_argument('--output',type=Path,default=ROOT/'data/fleet-short-links.json')
    parser.add_argument('--wait',action='store_true')
    args = parser.parse_args()
    if args.days!=sorted(set(args.days)):
        parser.error('Days must be unique and chronological.')
    run(args.scratch,args.days,args.output,args.wait)
