"""Paired replay of the street-level approaches actually linked by the crossing build."""

import argparse
import csv
import hashlib
from itertools import combinations
import io
import json
from pathlib import Path
import sys
import zipfile

import numpy as np
from scipy.spatial import cKDTree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from transit.walking import Walking, project


def summary(result):
    if result is None:
        return None
    return {k:result[k] for k in ('distance_m','seconds','uses_steps','inferred_crossing_access_m')}


def compare(data, schedule):
    before, after = Walking(data),Walking(data,use_crossing_access=True)
    manifest = (data/'walk-crossings.json').read_bytes()
    with zipfile.ZipFile(schedule) as archive:
        name=next(s for s in archive.namelist() if s.rsplit('/',1)[-1]=='stops.txt')
        stops=list(csv.DictReader(io.TextIOWrapper(archive.open(name),encoding='utf-8-sig')))
    positions=project([(float(s['stop_lat']),float(s['stop_lon'])) for s in stops])
    index=cKDTree(positions)
    records=[]
    for place in json.loads(manifest)['crossings']:
        tests=[]
        for a,b in combinations(place['approaches'],2):
            # Opposite sides only, using the precise nearby street vertices as
            # test pins. A terminal with four entrances can supply several pairs.
            u,v=project(a['landing']), project(b['landing'])
            if not before._crosses_barrier(u,v,np.linalg.norm(v-u)):
                continue
            start,end=a['street'],b['street']
            # Both modes of the bounded search are evaluated with identical limits.
            old=before.route(start,end,max_distance_m=2000)
            new=after.route(start,end,max_distance_m=2000)
            tests.append({'street_nodes':[a['street_node'],b['street_node']],
                          'before':summary(old),'after':summary(new)})
        nearest=int(index.query(project(place['approaches'][0]['landing']))[1])
        ref=stops[nearest]
        records.append({'osm_way':place['osm_way'],'landmark_nearest_gtfs_stop':ref['stop_name'],
                        'landmark_stop_id':ref['stop_id'],'tests':tests})
    aiims=[28.568914,77.213693]
    pins={'google_433_board':[28.569232,77.212737]}
    for s in stops:
        if s['stop_id'] in ('2839','2840'):
            pins[s['stop_id']]=[float(s['stop_lat']),float(s['stop_lon'])]
    cases={k:{'old':summary(before.route(aiims,p)),'new':summary(after.route(aiims,p))} for k,p in pins.items()}
    flat=[r for place in records for r in place['tests']]
    return {'method':'Every opposite-side street approach pair with an accepted crossing. A missing value means no route within the 2,000 m walking budget. These targeted checks measure graph connectivity, not passenger accuracy.',
            'manifest_sha256':hashlib.sha256(manifest).hexdigest(),'crossings':len(records),
            'opposite_side_pairs':len(flat),'reachable_before':sum(r['before'] is not None for r in flat),
            'reachable_after':sum(r['after'] is not None for r in flat),
            'both_reachable_and_shorter':sum(r['before'] is not None and r['after'] is not None and
                                             r['before']['distance_m']>r['after']['distance_m'] for r in flat),
            'aiims_unbounded_metres_and_seconds':cases,'checks':records}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data',type=Path,default=ROOT/'data')
    parser.add_argument('--schedule',type=Path,default=ROOT/'delhi_buses_static_gtfs_v1.zip')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    report=compare(args.data,args.schedule)
    args.output.write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    print({k:v for k,v in report.items() if k not in ('checks','aiims_unbounded_metres_and_seconds')})
