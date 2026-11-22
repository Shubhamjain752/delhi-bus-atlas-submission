"""Explain the two month-replay offers that retained a timely GPS exit report."""
import gzip
import json
import math
from pathlib import Path
import sys
import tempfile
from datetime import date

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tools.month_boarding_scan import ARCHIVE,PREVIOUS_EXTRACTS,daily_database
from tools.replay_peak_router import MonthRouter,prepared_rows,simulated_overlay
from tools.train_eta import open_db
from transit.replay import CapturedOverlay
from transit.router import approximate_m,coords


def main():
    records=[]
    engine=MonthRouter(root=ROOT)
    for day, route in [('2026-10-10','3214'),('2026-10-21','2183')]:
        with gzip.open(ROOT/'data'/'month-boarding'/(day+'.json.gz'),'rt') as f:
            data=json.load(f)
        receipt,=[a for a in data['router']['gone_offer_receipt_audit'] if a['route_id']==route
                  and a['both_received_by_ready'] and not a['suppressed']]
        row,=[r for r in data['opportunities'] if r['run']==receipt['run']]
        routed,=[r for r in data['router']['rows'] if r['run']==receipt['run']]
        with daily_database(Path('C:/Program Files/7-Zip/7z.exe'),ARCHIVE,data['archive_member'],PREVIOUS_EXTRACTS,
                            Path(tempfile.gettempdir())) as (db,source):
            with open_db(db) as cx:
                [(raw,reason,assignment)]=prepared_rows(cx,[row])
            assert reason is None
            overlay=simulated_overlay(engine.model,engine.data,engine.registry,row,routed['static_trip'],assignment[1],CapturedOverlay)
            pid,_=overlay.ids[routed['static_trip']]
            pat=engine.router.patterns[pid]
            i=pat.stops.index(row['stop'])
            ready=row['ready']
            result=overlay.passed_platform(date.fromisoformat(day),pid,i,(row['bus'],routed['static_trip']),ready)
            assert result is False
            track=overlay.exit_tracks[row['bus']][1]
            a,b=coords(engine.router.stops[row['stop']]),coords(engine.router.stops[row['next_stop']])
            scale=111195*math.cos(math.radians((a[0]+b[0])/2))
            dx,dy=(b[1]-a[1])*scale,(b[0]-a[0])*111195
            length=approximate_m(a,b)
            previous=None
            details=[]
            for observed,received,point in track:
                x,y=(point[1]-a[1])*scale,(point[0]-a[0])*111195
                along=(x*dx+y*dy)/length
                cross=abs(x*dy-y*dx)/length
                distance=math.hypot(x,y)
                margin=min(approximate_m(point,coords(engine.router.stops[s])) for s in pat.stops if s!=row['stop'])-distance
                details.append({'observed':observed,'received':received,'distance_m':round(distance,2),
                    'along_m':round(along,2),'change_along_m':round(along-previous,2) if previous is not None else None,
                    'cross_track_m':round(cross,2),'nearest_other_stop_margin_m':round(margin,2)})
                previous=along
            result_row={'day':day,'route':row['route'],'route_id':route,'ready':ready,'stop':row['stop'],
                        'stop_name':engine.router.stops[row['stop']]['name'],'next_stop':row['next_stop'],
                        'chord_length_m':round(length,2),'stop_count_in_pattern':pat.stops.count(row['stop']),
                        'next_stop_count_in_pattern':pat.stops.count(row['next_stop']),'veto':result,'receipt_audit':receipt,
                        'asof_track':details,'temporary_input_disposed':False}
            records.append(result_row)
        result_row['temporary_input_disposed']=source=='temporary_single_member' and not db.exists()
        assert result_row['temporary_input_disposed']
        print(json.dumps(result_row),flush=True)
    (ROOT/'data'/'month-retained-exits.json').write_text(json.dumps(records,indent=2),encoding='utf-8')


if __name__=='__main__':
    main()
