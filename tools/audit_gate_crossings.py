"""Find metro entrances near marked crossings. Write evidence for review only."""

import argparse
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import osmium
from scipy.spatial import cKDTree
from shapely import from_wkb, make_valid
from shapely.affinity import affine_transform
from shapely.geometry import LineString, MultiPoint, Point
from shapely.strtree import STRtree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.build_walk import NO, below_street_level, blocked_node, foot_directions
from transit.walking import MAX_CROSSING_APPROACH_M, ORIGIN, SCALE, project


def street_level(tags):
    return (not below_street_level(tags)
            and tags.get('layer', '0') in ('', '0') and tags.get('level', '0') in ('', '0')
            and tags.get('bridge', 'no') == 'no' and tags.get('tunnel', 'no') == 'no'
            and tags.get('indoor', 'no') == 'no' and tags.get('location') != 'overhead')


def marked(tags):
    markings = tags.get('crossing:markings')
    return (tags.get('crossing') in {'marked', 'zebra', 'pelican', 'toucan', 'tiger'}
            or bool(markings) and markings not in {'no', 'none', 'unmarked', 'unknown'})


def node_key(coord):
    # The OSM file format stores coordinates in units of 1e-7 degrees.
    return tuple(round(x * 1e7) for x in coord)


def graph_entrances(data):
    mask = json.loads((data / 'walk-surface.json').read_bytes())
    meta = json.loads((data / 'walk-meta.json').read_bytes())
    with np.load(data / 'walk.npz', allow_pickle=False) as graph:
        edges, coords = graph['edges'], graph['coords']
    if mask['source_md5'] != meta['input_md5'] or mask['edge_count'] != len(edges):
        raise ValueError('The walking graph and street-level index need to come from the same build.')
    underground = np.zeros(len(edges), dtype=bool)
    underground[mask['below_grade_edges']] = True
    entrances = mask['ground_entrance_nodes']
    surface_nodes = set(edges[~underground].ravel())
    # A coordinate shared by distinct graph nodes gives insufficient information.
    remaining = set()
    duplicate = set()
    for node in entrances:
        key = node_key(coords[node])
        if key in remaining:
            duplicate.add(key)
        remaining.add(key)
    match = {node_key(coords[i]): i not in surface_nodes for i in entrances
             if node_key(coords[i]) not in duplicate}
    return match, len(entrances), sum(i not in surface_nodes for i in entrances), meta['input_md5']


def read_gates(pbf, known):
    gates, marked_nodes = [], set()
    total = 0
    nodes = (osmium.FileProcessor(str(pbf)).with_filter(osmium.filter.EntityFilter(osmium.osm.NODE))
             .with_filter(osmium.filter.KeyFilter('railway', 'highway', 'crossing', 'crossing:markings')))
    for node in nodes:
        tags = node.tags
        if tags.get('highway') == 'crossing' and street_level(tags) and marked(tags):
            marked_nodes.add(node.id)
        if tags.get('railway') != 'subway_entrance':
            continue
        total += 1
        if blocked_node(tags) or not street_level(tags):
            continue
        coord = (node.location.lat, node.location.lon)
        matches = known.get(node_key(coord))
        gates.append({'id': node.id, 'name': tags.get('name') or tags.get('ref') or '',
                      'coord': coord, 'matched': matches is not None, 'only_underground': matches is True,
                      'endpoints': {}})
    return gates, total, marked_nodes


def marked_crossing(way, marked_nodes):
    tags = way.tags
    if (not street_level(tags) or foot_directions(tags) != 3
            or tags.get('footway') != 'crossing' and tags.get('path') != 'crossing'):
        return False
    if (len(way.nodes) < 2 or way.nodes[0].ref == way.nodes[-1].ref
            or tags.get('crossing') in {'no', 'unmarked', 'informal'}
            or tags.get('crossing:markings') in {'no', 'none', 'unmarked'}):
        return False
    return marked(tags) or any(n.ref in marked_nodes for n in way.nodes)


def screen(pbf, data):
    known, indexed, stairs_only, graph_checksum = graph_entrances(data)
    gates, total, marked_nodes = read_gates(pbf, known)
    positions = project([g['coord'] for g in gates]) if gates else np.empty((0, 2))
    tree = cKDTree(positions)
    count = 0
    ways = (osmium.FileProcessor(str(pbf)).with_locations()
            .with_filter(osmium.filter.EntityFilter(osmium.osm.WAY))
            .with_filter(osmium.filter.KeyFilter('footway', 'path')))
    for way in ways:
        if not marked_crossing(way, marked_nodes):
            continue
        count += 1
        for node in (way.nodes[0], way.nodes[-1]):
            if not node.location.valid():
                continue
            coord = (node.lat, node.lon)
            xy = project(coord)
            for k in tree.query_ball_point(xy, MAX_CROSSING_APPROACH_M):
                end = gates[k]['endpoints'].setdefault(node.ref, {
                    'node': node.ref, 'ways': [], 'coord': coord,
                    'metres': round(float(np.linalg.norm(xy - positions[k])), 2)})
                end['ways'].append(way.id)
    hits = [g for g in gates if g['endpoints']]
    for g in hits:
        g['endpoints'] = sorted(g['endpoints'].values(), key=lambda e: (e['metres'], e['node']))
    counts = {'release_indexed_gates': indexed, 'release_only_underground': stairs_only,
              'tagged_gates_in_extract': total, 'eligible_gates_in_extract': len(gates),
              'eligible_gates_matching_release': sum(g['matched'] for g in gates),
              'marked_crossing_ways': count, 'gates_near_crossing': len(hits),
              'matched_gates_near_crossing': sum(g['matched'] for g in hits),
              'only_underground_near_crossing': sum(g['only_underground'] for g in hits)}
    return {'release_graph_source_md5': graph_checksum, 'radius_metres': MAX_CROSSING_APPROACH_M,
            'counts': counts, 'gates_near_crossing': hits}


def obstacle(tags):
    if tags.get('building', 'no') != 'no' or tags.get('building:part', 'no') != 'no':
        return 'building'
    if tags.get('barrier', 'no') != 'no' or tags.get('foot') in NO or tags.get('access') in NO:
        return 'barrier or restricted access'
    if tags.get('waterway') or tags.get('water') or tags.get('natural') in {'water', 'wetland'}:
        return 'water'
    if tags.get('railway') in {'rail', 'light_rail', 'subway', 'tram', 'narrow_gauge'}:
        return 'railway'
    if tags.get('highway') and tags.get('highway') not in {
            'footway', 'pedestrian', 'path', 'steps', 'corridor', 'crossing', 'platform', 'elevator', 'cycleway'}:
        return 'road'
    if tags.get('area:highway') and tags.get('area:highway') not in {'footway', 'pedestrian', 'path', 'steps', 'platform'}:
        return 'road area'
    return None


def pedestrian_area(tags):
    return (not obstacle(tags)
            and (tags.get('area:highway') in {'footway', 'pedestrian'}
                 or tags.get('highway') in {'footway', 'pedestrian'} and tags.get('area') == 'yes'))


def inspect_approaches(pbf, gates):
    rows, lines = [], []
    for gate in gates:
        if not gate['only_underground']:
            continue
        for end in gate['endpoints']:
            rows.append({'gate_id': gate['id'], 'name': gate['name'], 'crossing_node': end['node'],
                         'crossing_ways': end['ways'], 'metres': end['metres'],
                         'mapped_obstacles': [], 'covering_pedestrian_areas': []})
            # WKB uses longitude, latitude. The project helper uses latitude, longitude.
            lines.append(LineString([gate['coord'][::-1], end['coord'][::-1]]))
    unreadable_areas = []
    if not rows:
        return rows, unreadable_areas
    tree = STRtree(lines)
    factory = osmium.geom.WKBFactory()
    # Assemble multipolygons to check their interiors and respect inner rings.
    features = (osmium.FileProcessor(str(pbf)).with_locations().with_areas()
                .with_filter(osmium.filter.KeyFilter('highway', 'area:highway', 'barrier', 'building',
                             'building:part', 'natural', 'water', 'waterway', 'railway', 'foot', 'access')))
    scale = [SCALE[1], 0, 0, SCALE[0], -SCALE[1] * ORIGIN[1], -SCALE[0] * ORIGIN[0]]
    metric_lines = [affine_transform(line, scale) for line in lines]
    for feature in features:
        tags = feature.tags
        if not street_level(tags):
            continue
        label = obstacle(tags)
        is_pedestrian_area = feature.is_area() and pedestrian_area(tags)
        if feature.is_node():
            if not blocked_node(tags):
                continue
            geom = Point(feature.location.lon, feature.location.lat)
            nearby = tree.query(geom.buffer(0.5 / min(SCALE)))
            for i in nearby:
                if metric_lines[i].distance(affine_transform(geom, scale)) <= 0.5:
                    rows[i]['mapped_obstacles'].append({'type': 'node', 'id': feature.id, 'feature': 'barrier or restricted access'})
            continue
        if not (label or is_pedestrian_area):
            continue
        if feature.is_area():
            osm_type = 'way' if feature.from_way() else 'relation'
            osm_id = feature.orig_id()
            try:
                geom = from_wkb(factory.create_multipolygon(feature))
            except RuntimeError:
                # Incomplete multipolygons can occur on the extract boundary.
                # Their footprint cannot confirm a walk. Flag overlapping approaches
                # conservatively using the extent of whatever coordinates remain.
                points = [(n.lon, n.lat) for ring in feature.outer_rings() for n in ring
                          if n.location.valid()]
                unreadable_areas.append({'type': osm_type, 'id': osm_id,
                                         'located': bool(points)})
                if not points:
                    continue
                geom = MultiPoint(points).envelope
                is_pedestrian_area = False
                label = 'incomplete area geometry'
        elif feature.is_way() and len(feature.nodes) >= 2 and all(n.location.valid() for n in feature.nodes):
            osm_type, osm_id = 'way', feature.id
            # Duplicate node positions occur in OSM ways. Ignore zero-length ways.
            if not any(n.ref != feature.nodes[0].ref for n in feature.nodes):
                continue
            try:
                geom = from_wkb(factory.create_linestring(feature))
            except RuntimeError:
                if len(set((n.lat, n.lon) for n in feature.nodes)) == 1:
                    continue
                raise
        else:
            continue
        nearby = tree.query(geom)
        if not len(nearby):
            continue
        if not geom.is_valid:
            geom = make_valid(geom)
        for i in nearby:
            if label and geom.intersects(lines[i]):
                hit = {'type': osm_type, 'id': osm_id, 'feature': label}
                if hit not in rows[i]['mapped_obstacles']:
                    rows[i]['mapped_obstacles'].append(hit)
            if is_pedestrian_area and geom.covers(lines[i]):
                rows[i]['covering_pedestrian_areas'].append({'type': osm_type, 'id': osm_id})
    return rows, unreadable_areas


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pbf', type=Path, required=True, help='OSM extract to inspect')
    parser.add_argument('--data', type=Path, default=ROOT / 'data', help='Atlas walking graph directory')
    parser.add_argument('--output', type=Path, required=True, help='directory for the two evidence files')
    parser.add_argument('--expect-sha256', help='fail if the extract differs from this digest')
    args = parser.parse_args()
    with args.pbf.open('rb') as src:
        digest = hashlib.file_digest(src, 'sha256').hexdigest()
    if args.expect_sha256 and digest != args.expect_sha256.lower():
        parser.error('The supplied extract does not match --expect-sha256.')
    with osmium.io.Reader(str(args.pbf)) as reader:
        timestamp = reader.header().get('osmosis_replication_timestamp')
    source = {'attribution': '© OpenStreetMap contributors, ODbL 1.0',
              'extract_sha256': digest, 'extract_replication_utc': timestamp}
    report = screen(args.pbf, args.data)
    approaches, unreadable_areas = inspect_approaches(args.pbf, report['gates_near_crossing'])
    args.output.mkdir(parents=True, exist_ok=True)
    evidence = {'approaches': approaches, 'incomplete_areas': unreadable_areas}
    for filename, result in [('gate-proximity.json', report), ('approach-checks.json', evidence)]:
        (args.output / filename).write_text(json.dumps(source | result, indent=2, ensure_ascii=False) + '\n',
                                           encoding='utf-8')
    print(json.dumps(report['counts'], indent=2))


if __name__ == '__main__':
    main()
