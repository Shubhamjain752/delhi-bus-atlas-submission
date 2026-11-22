"""Find moving, statically reconciled vehicle runs with observable stop entries in saved GTFS-RT polls."""

import argparse
from collections import defaultdict
from datetime import datetime
import gzip
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from transit.live import LiveOverlay, protobuf
from transit.router import Router, approximate_m, coords, IST


def inspect(folder, root):
    router=Router(root/'delhi_buses_static_gtfs_v1.zip',root/'data',None,None)
    live=LiveOverlay(None,root/'data').bind(router)
    tracks=defaultdict(dict)
    source_count=0
    received_files,raw_reports,identified_reports=[],0,0
    for fp in sorted(folder.glob('positions-*.pb.gz')):
        receipt=int(fp.name.split('-')[1])
        feed=protobuf().FeedMessage.FromString(gzip.decompress(fp.read_bytes()))
        if not 0<=receipt-feed.header.timestamp<=120:
            continue
        source_count+=1
        received_files.append(receipt)
        for ent in feed.entity:
            v=ent.vehicle
            desc=v.trip
            if not ent.HasField('vehicle') or ent.is_deleted:
                continue
            raw_reports+=1
            if (not v.vehicle.id or not desc.trip_id or not desc.start_date or desc.schedule_relationship!=0 or
                    not desc.route_id or not v.HasField('position') or not 0<=receipt-v.timestamp<=120):
                continue
            identified_reports+=1
            day=datetime.strptime(desc.start_date,'%Y%m%d').date()
            exact=live.ids.get(desc.trip_id)
            ids=[exact] if exact else live.by_clock.get((desc.route_id,live.clock(desc)),[])
            choices=[(pid,k) for pid,k in ids if router.patterns[pid].service in router._active(day)
                     and router.patterns[pid].route==desc.route_id]
            if len(choices)!=1:
                continue
            pid,k=choices[0]
            key=(v.vehicle.id,desc.route_id,desc.trip_id,desc.start_date,pid,k)
            tracks[key].setdefault(int(v.timestamp),(receipt,(v.position.latitude,v.position.longitude)))
    # Availability checks only: the scoring script separately replays these
    # receipts. Store the same vehicle, dated trip and actual named stops.
    updates=defaultdict(list)
    for fp in sorted(folder.glob('predictions-*.pb.gz')):
        receipt=int(fp.name.split('-')[1])
        feed=protobuf().FeedMessage.FromString(gzip.decompress(fp.read_bytes()))
        if not 0<=receipt-feed.header.timestamp<=120:
            continue
        for ent in feed.entity:
            update=ent.trip_update
            if ent.is_deleted or not ent.HasField('trip_update'):
                continue
            calls={c.stop_id for c in update.stop_time_update if c.schedule_relationship==0}
            run=(update.vehicle.id,update.trip.trip_id,update.trip.start_date)
            updates[run].append((receipt,calls))
    candidates=[]
    for (vid,rid,trip,date,pid,k),timeline in tracks.items():
        p=router.patterns[pid]
        indices=defaultdict(list)
        for idx,s in enumerate(p.stops):
            indices[s].append(idx)
        previous=None
        chain=[]
        for when,(receipt,pt) in sorted(timeline.items()):
            if previous is not None and (when-previous[0]>120 or
                    approximate_m(pt,previous[1])/(when-previous[0])>30):
                chain=[]
                previous=None
            near=sorted((approximate_m(pt,coords(router.stops[s])),s) for s in indices)
            dist,s=near[0]
            if (previous is not None and dist<=45 and len(indices[s])==1 and
                    (len(near)==1 or near[1][0]-dist>=12) and approximate_m(previous[1],coords(router.stops[s]))>45):
                i=indices[s][0]
                hit={'id':s,'index':i,'name':router.stops[s]['name'],'coords':list(coords(router.stops[s])),
                     'gps_time':when,'gps_ist':datetime.fromtimestamp(when,IST).isoformat(),'receipt':receipt,
                     'offset_m':round(dist,1),'before_time':previous[0],
                     'before_offset_m':round(approximate_m(previous[1],coords(router.stops[s])),1)}
                if chain and (i<=chain[-1]['index'] or i-chain[-1]['index']>8):
                    chain=[]
                for offset,first in enumerate(chain):
                    path=[(t,coordinate) for t,(_,coordinate) in sorted(timeline.items())
                          if first['gps_time']<=t<=when]
                    elapsed=when-first['gps_time']
                    meters=sum(approximate_m(a[1],b[1]) for a,b in zip(path,path[1:]))
                    gap=max(b[0]-a[0] for a,b in zip(path,path[1:]))
                    if meters>75 and 2<=3.6*meters/elapsed<=70 and gap<=120:
                        pre=[t for t,calls in updates[(vid,trip,date)]
                             if 0 < first['gps_time']-t <= 180 and {first['id'],hit['id']} <= calls]
                        events=chain[offset:]+[hit]
                        candidates.append({'route_id':rid,'route_name':router.routes[rid][0],'vehicle_id':vid,
                            'provider_trip':trip,'official_trip':p.trips[k],'service_date':date,
                            'departure_time':p.starts[k]+p.departures[0],'events':events,
                            'consecutive_static_stops':events[-1]['index']-events[0]['index']==len(events)-1,
                            'available_pre_origin_receipts':pre,
                            'receipts_before_previous_outside_fix':[t for t in pre if t < first['before_time']],
                            'elapsed_entry_bounds_seconds':[max(0,hit['before_time']-first['gps_time']),
                                                            when-first['before_time']],
                            'elapsed_seconds':elapsed,'moving_gps_distance_m':round(meters),
                            'max_ping_gap':gap,'continuous_pings':len(path)})
                chain.append(hit)
            previous=(when,pt)
    candidates.sort(key=lambda r:(not r['available_pre_origin_receipts'],-len(r['events']),
                                  not r['consecutive_static_stops'],-r['continuous_pings'],r['max_ping_gap'],
                                  sum(x['offset_m'] for x in r['events'])/len(r['events'])))
    def run_key(window):
        return window['vehicle_id'], window['provider_trip'], window['service_date']

    candidate_runs = {run_key(window) for window in candidates}
    pre_origin_runs = {
        run_key(window) for window in candidates if window['available_pre_origin_receipts']
    }
    strictly_pre_entry_runs = {
        run_key(window) for window in candidates if window['receipts_before_previous_outside_fix']
    }
    return {'polls':source_count,'first_receipt':min(received_files,default=None),'last_receipt':max(received_files,default=None),
            'raw_vehicle_reports':raw_reports,'fresh_reports_with_dated_service':identified_reports,
            'distinct_matched_pings':sum(len(v) for v in tracks.values()),
            'matched_vehicle_runs':len(tracks),'candidate_count':len(candidates),
            'distinct_runs_with_two_observed_stops':len(candidate_runs),
            'distinct_runs_with_pre_origin_updates_at_both_stops':len(pre_origin_runs),
            'distinct_runs_with_updates_before_previous_outside_fix':len(strictly_pre_entry_runs),
            'candidates':candidates}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder',type=Path,default=Path('data/nov21-samples'))
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    report=inspect(args.folder,Path(__file__).resolve().parents[1])
    if args.output:
        args.output.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps({**report,'candidates':report['candidates'][:2]},ensure_ascii=True))
