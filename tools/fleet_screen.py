"""Chronological, fixed-rule screening of complete route-hour GPS traces."""
from collections import Counter, defaultdict
from dataclasses import dataclass
import math

import numpy as np
from numba import njit

from tools.near_miss_peak import token

METRICS = ('unique_fixes','forward_stop_forecasts','near_miss_opportunities')


@dataclass
class Geometry:
    names: list
    arrays: tuple
    pattern_counts: dict


def geometry(router):
    """Prefer the route and provider start clock's pattern. Use a full pattern
    as a geographic screen where that clock cannot identify a unique pattern.
    The later production matcher must independently resolve the exact trip/date.
    """
    offsets, lat, lon, duration, phase, allowed, unique, names = [0],[],[],[],[],[],[],[]
    choices = defaultdict(list)
    preferred = np.full(65536,-1,'i4')
    longest = {}
    for pid,p in enumerate(router.patterns):
        r = int(p.route)
        if len(p.stops) > longest.get(r,0):
            preferred[r],longest[r] = pid,len(p.stops)
        for s in p.starts:
            minute = (s+p.departures[0])//60
            choices[r*10000+minute].append(pid)
            if minute >= 1440:
                choices[r*10000+minute%1440].append(pid)
        points = [router.stops[s] for s in p.stops]
        counts = Counter(p.stops)
        dists = [math.hypot((a['lat']-b['lat'])*111190,
                            (a['lon']-b['lon'])*111190*math.cos(math.radians((a['lat']+b['lat'])/2)))
                 for a,b in zip(points,points[1:])]
        for j,(s,point) in enumerate(zip(p.stops,points)):
            names.append(s)
            lat.append(point['lat'])
            lon.append(point['lon'])
            unique.append(counts[s] == 1)
            enough = j+2 < len(p.stops)
            duration.append(p.arrivals[j+1]-p.departures[j] if enough else -1)
            allowed.append(enough and all(counts[k]==1 for k in p.stops[j:j+3])
                           and min(dists[j:j+2]) >= 100 and not p.pickups[j+1] and not p.drops[j+2])
            phase.append(int(token(s,'platform-grid'),16)%120)
        offsets.append(len(names))
    keys = sorted(choices)
    # Resolve multiple schedules with identical stop geography via canonical longest, first pattern.
    pids = [max(set(choices[k]),key=lambda pid:(len(router.patterns[pid].stops),-pid)) for k in keys]
    return Geometry(names, (np.asarray(offsets,'i4'),np.asarray(lat),np.asarray(lon),
                             np.asarray(duration,'i4'),np.asarray(phase,'i4'),np.asarray(allowed),
                             np.asarray(unique),preferred,np.asarray(keys,'i8'),np.asarray(pids,'i4')),
                    {k:len(set(v)) for k,v in choices.items()})


@njit(cache=True)
def meter_delta(y, x, a, b, scale):
    return (y-a)*111190, (x-b)*scale


@njit(cache=True)
def nearest(y,x,start,end,lat,lon,scale):
    best,next_best = 1e30,1e30
    idx = -1
    for j in range(start,end):
        dy,dx = meter_delta(y,x,lat[j],lon[j],scale)
        square = dy*dy+dx*dx
        if square < best:
            idx,next_best,best = j,best,square
        elif square < next_best:
            next_best = square
    if best <= 45**2 and math.sqrt(next_best)-math.sqrt(best) >= 12:
        return idx
    return -1


@njit(cache=True,nogil=True)
def screen(a, core, offsets, lat, lon, duration, phase, allowed, unique, preferred, keys, pids):
    metrics = np.zeros((65536,3),'i8')
    candidates = []
    at = np.full(len(a),-2,'i4')  # -2 duplicate/out-of-order, -1 valid fix outside platform
    broken = np.ones(len(a),'?')
    prev,last_hit,pid,seq_start,seq_end = -1,-1,-1,0,0
    scale = 0.
    for i in range(len(a)):
        f = a[i]
        route = f.route
        new_run = (prev < 0 or f.bus != a[prev].bus or route != a[prev].route or f.trip != a[prev].trip)
        if new_run:
            prev,last_hit = -1,-1
            pid = preferred[route]
            if pid >= 0 and f.minute >= 0:
                key = route*10000+f.minute
                pos = np.searchsorted(keys,key)
                if pos < len(keys) and keys[pos]==key:
                    pid = pids[pos]
            if pid >= 0:
                seq_start,seq_end = offsets[pid],offsets[pid+1]
                scale = 111190*math.cos(math.radians(lat[seq_start]))
        elif f.obs <= a[prev].obs:
            continue
        if core <= f.recv < core+3600:
            metrics[route,0] += 1
        at[i] = -1
        if prev >= 0:
            gap = f.obs-a[prev].obs
            dy,dx = meter_delta(f.lat,f.lon,a[prev].lat,a[prev].lon,scale)
            broken[i] = gap>60 or dx*dx+dy*dy > (35*gap)**2
        if broken[i]:
            last_hit = -1
        if pid >= 0:
            j = nearest(f.lat,f.lon,seq_start,seq_end,lat,lon,scale)
            at[i] = j
            if j >= 0 and not broken[i] and j != at[prev]:
                dy,dx = meter_delta(a[prev].lat,a[prev].lon,lat[j],lon[j],scale)
                if (last_hit>=0 and 1<=j-at[last_hit]<=3 and f.obs-a[last_hit].obs<=600
                        and allowed[j] and 30<=duration[j]<=240 and dx*dx+dy*dy>45**2):
                    forecast = f.obs+duration[j]
                    ready = ((forecast-phase[j+1]+60)//120)*120 + phase[j+1]
                    if f.recv < ready and core <= ready < core+3600:
                        metrics[route,1] += 1
                        if 1 <= ready-forecast <= 30:
                            metrics[route,2] += 1
                            candidates.append((ready,i,j))
                if unique[j]:
                    last_hit = i
        prev = i
    return metrics, candidates, at, broken


@njit(cache=True)
def independent_label(a, anchor, idx, ready, at, broken, lat, lon):
    f = a[anchor]
    target = idx+1
    scale = 111190*math.cos(math.radians(lat[target]))
    prior,entry,entered = anchor,-1,-1
    target_y,target_x = lat[target],lon[target]
    next_dy,next_dx = meter_delta(lat[target+1],lon[target+1],target_y,target_x,scale)
    length = math.hypot(next_dx,next_dy)
    for i in range(anchor+1,len(a)):
        row = a[i]
        if row.bus!=f.bus or row.route!=f.route or row.trip!=f.trip:
            break
        if at[i]==-2:
            continue
        if broken[i] or row.obs-f.obs>720:
            break
        dy,dx = meter_delta(row.lat,row.lon,target_y,target_x,scale)
        if entered<0:
            if at[i]>=0 and (at[i]<idx or at[i]>target):
                break
            py,px = meter_delta(a[prior].lat,a[prior].lon,target_y,target_x,scale)
            if at[i]==target and px*px+py*py>45**2:
                entry,entered = prior,i
        elif dy*dy+dx*dx>45**2:
            if (dy*next_dy+dx*next_dx)/length>=20:
                label = 0 if ready<=a[entry].obs else (1 if ready>=row.obs else 2)
                return label,entry,entered,i
            break
        prior = i
    if entered>=0:
        return (0 if ready<=a[entry].obs else 2),entry,entered,-1
    return 3,-1,-1,-1
