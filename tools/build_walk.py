"""Build the offline Delhi pedestrian graph from a Geofabrik OSM PBF."""

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import tempfile
import urllib.request

import numpy as np


SOURCE_URL = ('https://download.geofabrik.de/asia/india/northern-zone/'
              'northern-zone-261101.osm.pbf')
SOURCE_MD5 = '66d5470f8fe1bce0b8820e8b99000c6b'
# south, west, north, east: NCT and adjacent NCR wherever the extract covers them.
BOUNDS = (28.25, 76.78, 28.96, 77.55)
MAJOR = {'motorway', 'trunk', 'primary', 'secondary'}
WALKABLE = {'footway','pedestrian','steps','path','living_street','residential',
            'unclassified','service','tertiary','tertiary_link','road','track'}
YES = {'yes','designated','official','permissive','destination'}
NO = {'no','private','use_sidepath','customers','permit','agricultural','forestry'}


def foot_directions(tags):
    """Return 1 for forward, 2 for backward, 3 for both, 0 for excluded."""
    highway = tags.get('highway', '')
    if not highway or tags.get('area') == 'yes' or tags.get('access:conditional') or tags.get('foot:conditional'):
        return 0
    foot = tags.get('foot')
    if foot in NO or (foot not in YES and tags.get('access') in NO):
        return 0
    if (highway in {'construction','proposed','abandoned','razed','raceway','motorway','motorway_link'} or
            tags.get('motorroad') == 'yes'):
        return 0
    if tags.get('sidewalk') == 'separate' or tags.get('sidewalk:both') == 'separate':
        return 0
    if highway not in WALKABLE and foot not in YES:
        sidewalk = tags.get('sidewalk') or tags.get('sidewalk:both')
        mapped_sidewalk = highway.removesuffix('_link') in MAJOR and sidewalk in {'yes','both','left','right'}
        # At-grade ordinary arterials are usable as a shared road centreline
        # estimate. Separated, explicitly sidewalk-free, elevated and restricted
        # carriageways require pedestrian evidence before routing over them.
        ordinary_arterial = (highway in {'primary','secondary'} and
                             tags.get('oneway') not in {'yes','true','1','-1'} and
                             tags.get('bridge') in {None,'no'} and tags.get('tunnel') in {None,'no'} and
                             sidewalk not in {'no','none','separate'})
        if not mapped_sidewalk and not ordinary_arterial:
            return 0
    direction = tags.get('oneway:foot', '')
    forward = direction not in {'-1','reverse'} and tags.get('foot:forward') not in NO
    backward = direction not in {'yes','true','1'} and tags.get('foot:backward') not in NO
    return int(forward) + 2 * int(backward)


def blocked_node(tags):
    if tags.get('foot') in YES:
        return False
    return (tags.get('foot') in NO or tags.get('access') in NO or
            tags.get('barrier') in {'wall','fence','retaining_wall','hedge','block'})


def barrier_way(tags):
    # Guard short endpoint connectors against crossing these lines. Crossings
    # that belong to the mapped network retain their explicit OSM topology.
    return (tags.get('highway','').removesuffix('_link') in MAJOR or
            tags.get('barrier') in {'wall','fence','retaining_wall','hedge'} or
            tags.get('railway') in {'rail','light_rail','subway'} or
            tags.get('waterway') in {'river','canal','drain'})


def inside(lat, lon):
    return BOUNDS[0] <= lat <= BOUNDS[2] and BOUNDS[1] <= lon <= BOUNDS[3]


def build(pbf, output, source_url, checksum):
    import osmium

    nodes = {}
    coords, edges, directions, factors, delays, barriers, centerlines = [], [], [], [], [], [], []
    blocked, signals = set(), set()
    classes = Counter()
    objects = (osmium.FileProcessor(str(pbf)).with_locations()
               .with_filter(osmium.filter.KeyFilter('highway','barrier','railway','waterway','access','foot','crossing')))
    for obj in objects:
        if obj.is_node():
            if not inside(obj.location.lat, obj.location.lon):
                continue
            if blocked_node(obj.tags):
                blocked.add(obj.id)
            if obj.tags.get('crossing') == 'traffic_signals' or obj.tags.get('crossing:signals') == 'yes':
                signals.add(obj.id)
            continue
        if not obj.is_way():
            continue
        tags = obj.tags
        direction = foot_directions(tags)
        barrier = barrier_way(tags) and tags.get('tunnel') != 'yes' and tags.get('bridge') != 'yes'
        if not (direction or barrier):
            continue
        previous = None
        for node in obj.nodes:
            if not node.location.valid() or not inside(node.lat,node.lon):
                previous = None
                continue
            current = (node.ref, node.lat, node.lon)
            if previous is not None:
                old_id, old_lat, old_lon = previous
                if barrier:
                    barriers.append((old_lat, old_lon, node.lat, node.lon))
                if direction and old_id != node.ref and old_id not in blocked and node.ref not in blocked:
                    pair = []
                    for osm_id, lat, lon in (previous, current):
                        if osm_id not in nodes:
                            nodes[osm_id] = len(coords)
                            coords.append((lat,lon))
                        pair.append(nodes[osm_id])
                    edges.append(pair)
                    directions.append(direction)
                    factors.append(1.5 if tags.get('highway') == 'steps' else 1.)
                    centerlines.append(tags.get('highway') in {'primary','secondary','tertiary','tertiary_link'}
                                       and tags.get('foot') not in YES and
                                       tags.get('sidewalk') not in {'both','yes','left','right'})
                    # Half a signal wait on entering and half on leaving the
                    # mapped crossing node; a through walk thus pays one wait.
                    delays.append(10 * (int(old_id in signals) + int(node.ref in signals)))
                    classes[tags.get('highway')] += 1
            previous = current
    if not edges:
        raise ValueError('No pedestrian network falls within the selected bounds')
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output,suffix='.npz',delete=False) as tmp:
        temp_name = tmp.name
        np.savez_compressed(tmp, coords=np.array(coords,dtype='f8'), edges=np.array(edges,dtype='i4'),
                            directions=np.array(directions,dtype='u1'), factors=np.array(factors,dtype='f4'),
                            delays=np.array(delays,dtype='f4'), barriers=np.array(barriers,dtype='f8').reshape(-1,4),
                            centerlines=np.asarray(centerlines,dtype='bool'))
    os.replace(temp_name, output/'walk.npz')
    metadata = {'attribution':'© OpenStreetMap contributors, extract by Geofabrik',
                'license':'ODbL 1.0', 'license_url':'https://www.openstreetmap.org/copyright',
                'source':source_url, 'input_md5':checksum, 'clip_bounds_swne':BOUNDS,
                'nodes':len(coords), 'edges':len(edges),'connector_barriers':len(barriers),
                'edge_counts_by_highway':dict(classes),'walk_speed_m_s':1.25,
                'steps_time_multiplier':1.5,'mapped_signal_wait_seconds':20,'max_snap_metres':65}
    (output/'walk-meta.json').write_text(json.dumps(metadata,indent=2),encoding='utf-8')
    print(json.dumps(metadata,indent=2,ensure_ascii=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pbf', type=Path, help='local Geofabrik Northern Zone extract')
    parser.add_argument('--output', type=Path, default=Path('data'))
    parser.add_argument('--source-url', default=SOURCE_URL)
    parser.add_argument('--md5', default=SOURCE_MD5, help='provider checksum for the selected extract')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as cache:
        path = args.pbf or Path(cache)/'northern-zone.osm.pbf'
        if not args.pbf:
            urllib.request.urlretrieve(args.source_url, path)
        with path.open('rb') as data:
            checksum = hashlib.file_digest(data,'md5').hexdigest()
        if checksum.lower() != args.md5.lower():
            raise ValueError('OSM extract checksum differs from --md5')
        build(path, args.output, args.source_url, checksum)


if __name__ == '__main__':
    main()
