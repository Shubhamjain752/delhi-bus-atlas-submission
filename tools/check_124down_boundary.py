"""Verify the single timely-bracket gone offer in the frozen October confirmation replay."""
import argparse
from contextlib import contextmanager, closing
from datetime import date, datetime
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
import struct
import subprocess
import sys
import tempfile
import zlib

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tools.fleet_positions import received_positions
from transit.live import protobuf
from transit.replay import CapturedOverlay
from transit.router import Router, IST, coords, approximate_m

DAY='2026-10-30'
ARCHIVE='delhi-dtc-bus-gtfs-rt-2026-10.7z'


def stamp(epoch):
    return datetime.fromtimestamp(epoch,IST).isoformat()


def key_for(value):
    h=14695981039346656037
    for byte in value.encode():
        h=((h ^ byte)*1099511628211) & 0xffffffffffffffff
    return h


def token(*parts):
    return hashlib.blake2s('|'.join(map(str,parts)).encode(),digest_size=8).hexdigest()


@contextmanager
def source_db(path, seven, info):
    if path is None:
        with tempfile.NamedTemporaryFile(prefix='delhi-124-check-',suffix='.db',delete=True,delete_on_close=False) as f:
            subprocess.run([str(seven),'x','-so',str(ROOT/ARCHIVE),info['member']],stdout=f,check=True)
            f.flush()
            with source_db(Path(f.name),seven,info) as db:
                yield db
        return
    if path.stat().st_size!=info['bytes']:
        raise ValueError('The supplied SQLite member size differs from the archive census.')
    crc=0
    with path.open('rb') as f:
        for block in iter(lambda:f.read(4*1024**2),b''):
            crc=zlib.crc32(block,crc)
    if f'{crc:08X}'!=info['crc32']:
        raise ValueError('The SQLite member CRC differs from the archive census.')
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro&immutable=1',uri=True)) as db:
        yield db


def extract(db,row):
    def boundary(clock):
        lo,hi=1,db.execute('select max(rowid)+1 from vehicle_feed').fetchone()[0]
        while lo<hi:
            mid=(lo+hi)//2
            if db.execute('select time from vehicle_feed where rowid=?',(mid,)).fetchone()[0]>=clock:
                hi=mid
            else:
                lo=mid+1
        return lo
    anchor=stamp(row['forecast_receipt'])[11:19]
    exact=db.execute('select vehicle_id,trip_id from vehicle_feed where rowid>=? and rowid<? and route_id=? '
                    'and cast(timestamp as int)=?',(boundary(anchor),boundary(stamp(row['forecast_receipt']+1)[11:19]),
                                                   int(row['route_id']),row['origin_gps']))
    identities={(v,t) for v,t in exact if token(key_for(v),'fleet-bus')==row['bus'] and
                token(DAY,key_for(v),key_for(t),'fleet-run')==row['run']}
    if len(identities)!=1:
        raise ValueError('Raw physical vehicle/run is not uniquely identified.')
    vehicle,trip=identities.pop()
    records=db.execute('select time,route_id,trip_id,cast(timestamp as int),cast(lat as real),cast(lng as real) '
        'from vehicle_feed where rowid>=? and rowid<? and vehicle_id=? order by rowid',
        (boundary('05:18:00'),boundary('05:27:00'),vehicle))
    points=[]
    for wall,route,tid,obs,y,x in records:
        recv=int(datetime.fromisoformat(DAY+'T'+wall).replace(tzinfo=IST).timestamp())
        if tid==trip and route==int(row['route_id']) and 0<=recv-obs<=90:
            points.append((recv,obs,y,x))
    if not set(map(tuple,row['observations']))<=set(points):
        raise ValueError('One or more historic as-of records differs from the source.')
    return points


def policy(router, registry, records, trip, index, cutoff):
    live=CapturedOverlay(None,ROOT/'data',cutoff)
    for attr in ('router','ids','by_clock','edges'):
        setattr(live,attr,getattr(registry,attr))
    for recv,fixes in received_positions([r for r in records if r[0]<=cutoff]):
        feed=protobuf().FeedMessage()
        feed.header.gtfs_realtime_version,feed.header.timestamp='2.0',recv
        for n,(obs,lat,lon) in enumerate(fixes):
            v=feed.entity.add(id=str(n)).vehicle
            v.vehicle.id='case-vehicle'
            v.trip.route_id,v.trip.trip_id,v.trip.start_date='1563',trip,DAY.replace('-','')
            v.timestamp=obs
            v.position.latitude,v.position.longitude=lat,lon
        live.apply_positions(feed,recv,provider='community_positions')
    args=date.fromisoformat(DAY),registry.ids[trip][0],index,('case-vehicle',trip),cutoff
    age,approach=live.boarding_position(*args)
    return {'asof':stamp(cutoff),'passed_platform':bool(live.passed_platform(*args)),
            'positive_approach':bool(approach),'gps_age_seconds':age}


def run(db_path,seven):
    data=ROOT/'data'
    case_path=data/'fleet-cases'/f'{DAY}.json.gz'
    row=next(r for r in json.load(gzip.open(case_path))['cases'] if r['route_id']=='1563' and r['hour']==5)
    pair=next(r for r in json.load(gzip.open(data/'fleet-router/holdout.json.gz'))['cases']
              if r['day']==DAY and r['route_id']=='1563' and r['hour']==5)
    meta=json.loads((data/'month-boarding/manifest.json').read_text())
    info=meta['archive_members'][DAY]
    with source_db(db_path,seven,info) as db:
        raw=extract(db,row)
    router=Router(ROOT/'delhi_buses_static_gtfs_v1.zip',data,None,None)
    registry=CapturedOverlay(None,data,0).bind(router)
    trip=pair['trip']
    p=router.patterns[registry.ids[trip][0]]
    i=p.stops.index(row['stop'])
    here,onward=router.stops[row['stop']],router.stops[row['next_stop']]
    path=router.platform_path(registry.ids[trip][0],i)
    by_obs={}
    for recv,obs,y,x in raw:
        by_obs.setdefault(obs,(recv,struct.unpack('ff',struct.pack('ff',y,x))))
    def point(obs,kind):
        recv,pt=by_obs[obs]
        along=path.project(pt) if path else None
        return {'event':kind,'observed_at':stamp(obs),'first_received_at':stamp(recv),
                'received_by_readiness':recv<=row['ready'],'distance_to_platform_m':round(approximate_m(pt,coords(here)),2),
                'distance_to_next_stop_m':round(approximate_m(pt,coords(onward)),2),
                'along_published_shape_from_platform_m':None if along is None else round(along,2)}
    before,entry,first_exit=row['gps_entry_exit']
    ordered=sorted(by_obs)
    last_in=max(t for t in ordered if entry<=t<first_exit and approximate_m(by_obs[t][1],coords(here))<=45)
    reenter=next(t for t in ordered if first_exit<t<=row['ready'] and by_obs[t][0]<=row['ready']
                 and approximate_m(by_obs[t][1],coords(here))<=45)
    confirm=next(t for t in ordered if reenter<t<=row['ready']<by_obs[t][0]
                 and approximate_m(by_obs[t][1],coords(here))>45)
    later=next(t for t in ordered if t>row['ready'])
    next_stop=next(t for t in ordered if t>row['ready'] and approximate_m(by_obs[t][1],coords(onward))<=45)
    receipt=by_obs[confirm][0]
    # Keep the exact as-of prefix supplied to the frozen router. Then add real later receipts.
    stream=list(row['observations'])+[fix for fix in raw if row['ready']<fix[0]<=receipt]
    checks=[policy(router,registry,stream,trip,i,t) for t in (row['ready'],receipt-1,receipt)]
    assert [r['passed_platform'] for r in checks]==[False,False,True]
    assert pair['outcome']=='gone' and pair['revised']['offered'] and not pair['revised']['vehicle_approaching']
    with case_path.open('rb') as f:
        case_digest=hashlib.file_digest(f,'sha256').hexdigest()
    return {'day':DAY,'route_id':row['route_id'],'route':router.routes[row['route_id']][0],
            'platform':{'id':here['id'],'name':here['name']},'next_stop':{'id':onward['id'],'name':onward['name']},
            'shape_id':p.shape,'timetable_trip':trip,'ready_at':stamp(row['ready']),
            'forecast_departure_at':stamp(row['forecast']),'gap_seconds':row['gap'],'gps_label':pair['outcome'],
            'baseline_offer':pair['baseline']['offered'],'revised_offer':pair['revised']['offered'],
            'revised_display':'secondary conditional; ordinary fallback first','asof_raw_records_verified':len(row['observations']),
            'source_archive':ARCHIVE,'source_archive_sha256':meta['archive_digest_from_supplied_checksums'],
            'archive_member':info['member'],'expanded_bytes':info['bytes'],'verified_member_crc32':info['crc32'],
            'cohort_member_sha256':case_digest,'case_lookup':{'study':'holdout','day':DAY,'route_id':'1563','civil_hour_ist':5},
            'track':[point(t,k) for t,k in [(before,'before_entry'),(entry,'entered_45_m'),(last_in,'last_inside_before_first_exit'),
                     (first_exit,'first_45_m_exit'),(reenter,'back_inside'),(confirm,'outside_before_ready_reported_later'),
                     (later,'continued_downstream'),(next_stop,'entered_next_stop_45_m')]],
            'production_exit_replay':checks,'interpretation':'The later fix confirms downstream movement before readiness. '
            'Its receipt came after the decision. Door operation and passenger boarding are unobserved.',
            'reproduce':'python tools/check_124down_boundary.py (or pass --database PATH to a previously expanded October 30 SQLite member)'}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',type=Path)
    parser.add_argument('--seven',type=Path,default=Path('C:/Program Files/7-Zip/7z.exe'))
    parser.add_argument('--output',type=Path,default=ROOT/'data/fleet-124down-check.json')
    a=parser.parse_args()
    result=run(a.database,a.seven)
    tmp=a.output.with_suffix('.pending')
    tmp.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    tmp.replace(a.output)
    print(json.dumps(result['production_exit_replay']))
