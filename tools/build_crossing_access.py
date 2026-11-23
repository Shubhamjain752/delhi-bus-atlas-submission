"""Find missing street approaches to explicitly mapped public stair crossings."""

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
from scipy.spatial import cKDTree
from shapely.geometry import LineString, Point, Polygon

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tools.build_walk import NO, barrier_way, blocked_node, foot_directions
from transit.walking import MAX_CROSSING_APPROACH_M, Walking, project


STREETS = {'footway','pedestrian','path','living_street','service','residential',
           'unclassified','tertiary'}


def grade(tags):
    if tags.get('tunnel') not in (None,'no') or tags.get('covered') == 'underground':
        return 'tunnel'
    if tags.get('bridge') not in (None,'no'):
        return 'bridge'
    return None


def blocked_area(tags):
    return (tags.get('building') not in (None,'no') or tags.get('natural') == 'water'
            or 'water' in tags or tags.get('access') in NO or tags.get('foot') in NO)


def records(way):
    return [(n.ref,(n.lat,n.lon)) for n in way.nodes if n.location.valid()]


def crossing_candidates(pbf, walk):
    import osmium
    stairs_by_end = defaultdict(list)
    crossings, boundary_ways = [], set()
    selected=(osmium.FileProcessor(str(pbf)).with_locations()
              .with_filter(osmium.filter.KeyFilter('highway','building','natural','water','access','foot')))
    for item in selected:
        if item.is_relation() and blocked_area(item.tags):
            boundary_ways.update(m.ref for m in item.members if m.type == 'w' and m.role in ('','outer'))
        if not item.is_way() or item.tags.get('highway') not in ('steps','footway','pedestrian','path'):
            continue
        tags = dict(item.tags)
        if foot_directions(tags) != 3 or tags.get('indoor') == 'yes':
            continue
        pts = records(item)
        if len(pts)<2:
            continue
        if tags['highway'] == 'steps' and tags.get('incline') in ('up','down'):
            for ix,other in ((0,-1),(-1,0)):
                # Store the junction with the crossing and the opposite street end.
                upper = (tags['incline'] == 'down') == (other==0)
                stairs_by_end[pts[ix][0]].append((item.id,pts[other],pts[1 if other==0 else -2],upper))
        elif tags['highway'] != 'steps' and grade(tags) and len(pts)>1:
            crossings.append((item.id,grade(tags),pts[0][0],pts[-1][0]))

    degree = np.bincount(walk.edges.ravel(),minlength=len(walk.coords))
    sizes = np.bincount(walk.components)
    candidates = []
    for way, kind, first, last in crossings:
        if first==last:
            continue
        local = []
        for end in (first,last):
            for steps_id, (node,xy), (_,adjacent), is_upper in stairs_by_end.get(end,[]):
                if is_upper != (kind=='tunnel'):
                    continue
                graph = walk.mapped_node(xy,adjacent)
                if graph is not None and degree[graph]==1 and sizes[walk.components[graph]]<=80:
                    local.append(dict(crossing_way=way, end=end, kind=kind, steps_way=steps_id,
                                      landing_node=node,landing=xy,steps_neighbour=adjacent,graph=graph))
        if len({x['end'] for x in local})==2:
            candidates.extend(local)
    return candidates,boundary_ways


def sample_neighbourhoods(pbf, candidates, boundaries):
    """Read only public ground streets and obstructing mapped features near the stairs."""
    import osmium
    positions = np.array([project(c['landing']) for c in candidates])
    locations = cKDTree(positions)
    streets, walls = defaultdict(list),defaultdict(list)
    selected=(osmium.FileProcessor(str(pbf)).with_locations()
              .with_filter(osmium.filter.EntityFilter(osmium.osm.NODE | osmium.osm.WAY)))
    for obj in selected:
        if obj.is_node():
            if not blocked_node(obj.tags):
                continue
            point=(obj.location.lat,obj.location.lon)
            for k in locations.query_ball_point(project(point),MAX_CROSSING_APPROACH_M):
                walls[k].append(Point(point).buffer(.000002))
            continue
        if not obj.is_way():
            continue
        t=obj.tags
        highway=t.get('highway')
        # Grade-separated overhead features may cross a ground approach in plan.
        if grade(t) or t.get('layer','0') not in ('0','') or t.get('indoor') == 'yes':
            continue
        area = blocked_area(t) or obj.id in boundaries
        if not (highway or area or barrier_way(t)):
            continue
        pts=records(obj)
        if len(pts)<2:
            continue
        coords=np.array([p for _,p in pts])
        lo,hi=project(coords.min(axis=0)),project(coords.max(axis=0))
        middle=(lo+hi)/2
        near=locations.query_ball_point(middle, np.linalg.norm(hi-lo)/2+MAX_CROSSING_APPROACH_M)
        if not near:
            continue
        shape=(Polygon(coords) if area and len(coords)>3 and pts[0][0]==pts[-1][0]
               else LineString(coords))
        if not shape.is_valid:
            shape=shape.buffer(0) if shape.geom_type=='Polygon' else shape
        target = (highway in STREETS and t.get('service') not in ('driveway','parking_aisle')
                  and t.get('area') != 'yes' and foot_directions(t)==3 and not area)
        for k in near:
            if np.any(positions[k]<lo-MAX_CROSSING_APPROACH_M) or np.any(positions[k]>hi+MAX_CROSSING_APPROACH_M):
                continue
            walls[k].append(shape)
            if target:
                for i, (node,pt) in enumerate(pts):
                    if np.linalg.norm(positions[k]-project(pt))<=MAX_CROSSING_APPROACH_M:
                        streets[k].append((node,pt,pts[i-1 if i else 1][1],obj.id))
    return streets,walls


def approach(walk,candidate,streets,obstacles):
    surface=candidate['landing']
    start=project(surface)
    options=sorted(streets, key=lambda s:np.linalg.norm(project(s[1])-start))
    for node,point,next_point,way in options:
        street=walk.mapped_node(point,next_point)
        if street is None or walk.components[street]==walk.components[candidate['graph']]:
            continue
        length=np.linalg.norm(project(point)-start)
        if length<1 or length>MAX_CROSSING_APPROACH_M or walk._crosses_barrier(start,project(point),length):
            continue
        segment=LineString([surface,point])
        # The ground connector may meet the chosen street at its endpoint only.
        interior=LineString([segment.interpolate(f,normalized=True) for f in (.0001,.9999)])
        if any(interior.intersects(obstacle) for obstacle in obstacles):
            continue
        return {key:candidate[key] for key in ('landing_node','landing','steps_way','steps_neighbour')} | {
            'street_node':node,'street':point,'street_neighbour':next_point,'street_way':way,
            'estimated_metres':round(float(length),1)}
    return None


def build(pbf,data,source):
    import osmium
    walk=Walking(data,use_crossing_access=False)
    candidates,boundaries=crossing_candidates(pbf,walk)
    streets,obstacles=sample_neighbourhoods(pbf,candidates,boundaries) if candidates else ({},{})
    linked=defaultdict(dict)
    for k,candidate in enumerate(candidates):
        link=approach(walk,candidate,streets.get(k,[]),obstacles.get(k,[]))
        if link:
            sides=linked[candidate['crossing_way']]
            sides.setdefault(candidate['end'],[]).append(link)
    result=[]
    for way,sides in sorted(linked.items()):
        if len(sides)!=2:
            continue
        side1,side2=sides.values()
        spans_barrier=False
        for a in side1:
            for b in side2:
                u,v=project(a['landing']),project(b['landing'])
                if walk._crosses_barrier(u,v,np.linalg.norm(v-u)):
                    spans_barrier=True
        if spans_barrier:
            result.append({'osm_way':way,'approaches':side1+side2})
    with pbf.open('rb') as f:
        checksum=hashlib.file_digest(f,'sha256').hexdigest()
    with osmium.io.Reader(str(pbf)) as reader:
        timestamp=reader.header().get('osmosis_replication_timestamp')
    return dict(source=source, source_sha256=checksum, source_replication_utc=timestamp,
                attribution='© OpenStreetMap contributors, ODbL 1.0',
                max_estimated_link_m=MAX_CROSSING_APPROACH_M,
                candidate_crossings=len({c['crossing_way'] for c in candidates}), crossings=result)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pbf',type=Path,required=True)
    parser.add_argument('--data',type=Path,default=ROOT/'data')
    parser.add_argument('--source',required=True)
    args=parser.parse_args()
    evidence=build(args.pbf,args.data,args.source)
    target=args.data/'walk-crossings.json'
    target.with_suffix('.tmp').write_text(json.dumps(evidence,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    target.with_suffix('.tmp').replace(target)
    print(f"{len(evidence['crossings'])} crossings with matching approaches in {target}")
