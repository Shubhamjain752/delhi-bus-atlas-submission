"""Walk over shared OSM node topology; endpoint connectors are approximate."""

from functools import lru_cache
from heapq import heappop, heappush
import json
import math
from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components


SPEED = 1.25
MAX_SNAP = 65.
MAX_CROSSING_APPROACH_M = 40.
# Local equirectangular coordinates in metres, centred within the Delhi extract.
SCALE = np.array([111195., 111195. * math.cos(math.radians(28.60))])
ORIGIN = np.array([28.60, 77.20])


def project(points):
    return (np.asarray(points) - ORIGIN) * SCALE


class SegmentIndex:
    """Sample each segment's midpoint at intervals up to 30 m for exact radius retrieval."""

    def __init__(self, starts, ends):
        self.starts = starts
        self.vectors = ends - starts
        self.squared_lengths = np.einsum('ij,ij->i', self.vectors, self.vectors)
        counts = np.maximum(1, np.ceil(np.sqrt(self.squared_lengths) / 30).astype(int))
        self.ids = np.repeat(np.arange(len(starts),dtype='i4'), counts)
        offsets = np.repeat(np.cumsum(counts) - counts, counts)
        fractions = (np.arange(len(self.ids)) - offsets + .5) / counts[self.ids]
        self.tree = cKDTree(starts[self.ids] + self.vectors[self.ids] * fractions[:,None])

    def nearby(self, point, radius):
        return np.unique(self.ids[self.tree.query_ball_point(point, radius + 15.01)])


def cross(a, b):
    return a[...,0] * b[...,1] - a[...,1] * b[...,0]


class Walking:
    """Offline street routes. Coordinates and line geometry are (latitude, longitude)."""

    def __init__(self, data_dir: Path, *, use_crossing_access=False):
        data_dir = Path(data_dir)
        with np.load(data_dir/'walk.npz',allow_pickle=False) as data:
            self.coords = data['coords']
            self.edges = data['edges'].astype('i4', copy=False)
            self.directions = data['directions']
            self.xy = project(self.coords)
            self.index = SegmentIndex(self.xy[self.edges[:,0]], self.xy[self.edges[:,1]])
            self.length = np.sqrt(self.index.squared_lengths)
            self.cost = self.length * data['factors'] + data['delays'] * SPEED
            self.steps = data['factors'] > 1
            self.centerlines = data['centerlines'] if 'centerlines' in data else np.zeros(len(self.edges),dtype=bool)
            barriers = data['barriers'].reshape(-1,4)
            self.obstacles = SegmentIndex(project(barriers[:,:2]), project(barriers[:,2:]))
        info_path = data_dir/'walk-meta.json'
        self.metadata = json.loads(info_path.read_text(encoding='utf-8')) if info_path.exists() else {}
        self.mapped_edge_count = len(self.edges)
        evidence = data_dir/'walk-crossings.json'
        if use_crossing_access and evidence.is_file():
            self._add_crossing_approaches(json.loads(evidence.read_text(encoding='utf-8')))
        # CSR-style adjacency stores edges compactly while preserving parallel
        # ways and foot-only direction even when two roads share endpoints.
        forward = np.flatnonzero(self.directions & 1)
        reverse = np.flatnonzero(self.directions & 2)
        from_nodes = np.r_[self.edges[forward,0], self.edges[reverse,1]]
        order = np.argsort(from_nodes,kind='stable')
        self.to_nodes = np.r_[self.edges[forward,1], self.edges[reverse,0]][order]
        self.weights = self.cost[np.r_[forward,reverse][order]]
        self.arc_edges = np.r_[forward,reverse][order]
        self.offsets = np.r_[0, np.cumsum(np.bincount(from_nodes,minlength=len(self.coords)))]
        topology = csr_matrix((self.weights,self.to_nodes,self.offsets),shape=(len(self.coords),len(self.coords)))
        _, self.components = connected_components(topology,directed=False)
        self._snap = lru_cache(maxsize=30000)(self._nearest)
        self._cached_route = lru_cache(maxsize=1024)(self._route)

    def mapped_node(self, point, neighbour):
        """Find an unambiguous source edge, including its level at overlapping nodes."""
        point_xy = project(point)
        matches = set()
        for i in self.index.nearby(point_xy, .2):
            a, b = self.edges[i]
            for n, other in ((a,b),(b,a)):
                if (np.abs(self.coords[n]-point).max() < 5e-8 and
                    np.abs(self.coords[other]-neighbour).max() < 5e-8):
                    matches.add(int(n))
        return matches.pop() if len(matches) == 1 else None

    def _add_crossing_approaches(self, evidence):
        # Keep synthetic approaches out of the spatial snapping index. Attach only
        # when both original source edges still exist in this version of the map.
        links = set()
        for crossing in evidence['crossings']:
            for item in crossing['approaches']:
                a = self.mapped_node(item['landing'],item['steps_neighbour'])
                b = self.mapped_node(item['street'],item['street_neighbour'])
                if a is None or b is None:
                    continue
                length = np.linalg.norm(self.xy[a]-self.xy[b])
                if 0 < length <= MAX_CROSSING_APPROACH_M and not self._crosses_barrier(self.xy[a],self.xy[b],length):
                    links.add((a,b))
        if not links:
            return
        extra = np.asarray(sorted(links),dtype=self.edges.dtype)
        lengths = np.linalg.norm(self.xy[extra[:,0]]-self.xy[extra[:,1]],axis=1)
        self.edges = np.concatenate((self.edges,extra))
        self.directions = np.r_[self.directions,np.full(len(extra),3,dtype='u1')]
        self.cost = np.r_[self.cost,lengths]
        self.length = np.r_[self.length,lengths]
        self.centerlines = np.r_[self.centerlines,np.zeros(len(extra),dtype=bool)]
        self.steps = np.r_[self.steps,np.zeros(len(extra),dtype=bool)]

    def _crosses_barrier(self, point, mapped, distance):
        if distance < 1. or not len(self.obstacles.starts):
            return False
        candidates = self.obstacles.nearby((point + mapped) / 2, distance / 2)
        if not len(candidates):
            return False
        segment = mapped - point
        delta = self.obstacles.starts[candidates] - point
        walls = self.obstacles.vectors[candidates]
        denom = cross(segment,walls)
        ok = np.abs(denom) > 1e-8
        if not ok.any():
            return False
        t = cross(delta[ok],walls[ok]) / denom[ok]
        u = cross(delta[ok],segment) / denom[ok]
        # An endpoint on the road or wall line is allowed, as GTFS positions
        # may coincide with the roadway. Intervening barriers block snapping.
        return bool(((t > .001) & (t < .999) & (u >= 0.) & (u <= 1.)).any())

    def _nearest(self, coord):
        point = project(coord)
        candidates = self.index.nearby(point, MAX_SNAP)
        if not len(candidates):
            return None
        vectors = self.index.vectors[candidates]
        starts = self.index.starts[candidates]
        lengths = self.index.squared_lengths[candidates]
        numer = np.einsum('ij,ij->i',point-starts,vectors)
        fractions = np.clip(np.divide(numer,lengths,out=np.zeros_like(numer),where=lengths>0),0,1)
        mapped = starts + vectors * fractions[:,None]
        distances = np.linalg.norm(mapped - point,axis=1)
        for i in np.argsort(distances):
            if distances[i] > MAX_SNAP:
                break
            if not self._crosses_barrier(point,mapped[i],distances[i]):
                return int(candidates[i]), float(fractions[i]), float(distances[i]), mapped[i]
        return None

    def route(self, origin, destination, max_distance_m=None):
        """Return route or None (outside coverage, disconnected or over caller's walking limit)."""
        start = tuple(map(float,origin))
        end = tuple(map(float,destination))
        if len(start) != 2 or len(end) != 2 or not all(map(math.isfinite,(*start,*end))):
            return None
        if max_distance_m is not None and max_distance_m < 0:
            return None
        return self._cached_route(start,end,max_distance_m)

    def _ends(self, match, arriving):
        edge, t, _, _ = match
        forward, backward = self.directions[edge] & 1, self.directions[edge] & 2
        fractions = {}
        if (forward if arriving else backward) or t < 1e-8:
            fractions[int(self.edges[edge,0])] = t * self.cost[edge]
        if (backward if arriving else forward) or t > 1-1e-8:
            fractions[int(self.edges[edge,1])] = (1-t) * self.cost[edge]
        return fractions

    def _route(self, start, end, limit):
        if limit is not None and np.linalg.norm(project(start)-project(end)) > limit:
            return None
        a, b = self._snap(start), self._snap(end)
        if a is None or b is None:
            return None
        if self.components[self.edges[a[0],0]] != self.components[self.edges[b[0],0]]:
            return None
        if start == end:
            return {'seconds':0, 'distance_m':0, 'geometry':[list(start)],
                    'source':'OpenStreetMap pedestrian network',
                    'origin_snap_m':round(a[2],1), 'destination_snap_m':round(b[2],1),
                    'uses_steps':False, 'inferred_crossing_access_m':0,
                    'includes_arterial_without_mapped_sidewalk':False}
        best_cost = math.inf
        best_node = None
        if a[0] == b[0]:
            delta = b[1]-a[1]
            direction = self.directions[a[0]]
            if abs(delta) < 1e-10 or (delta > 0 and direction & 1) or (delta < 0 and direction & 2):
                best_cost = abs(delta)*self.cost[a[0]]
        goals = self._ends(b,True)
        scores = self._ends(a,False)
        parents = {}
        queue = []
        target_y, target_x = b[3]

        def remaining(n):
            return math.hypot(self.xy[n,0]-target_y,self.xy[n,1]-target_x)

        for n, score in scores.items():
            heappush(queue,(score+remaining(n),score,n))
        # The distance-only admissible heuristic includes neither crossing waits
        # nor stair penalties. Restrict bounded walking searches conservatively.
        search_limit = math.inf if limit is None else max(0.,limit)*3 + 200
        while queue:
            bound, score, n = heappop(queue)
            if bound >= best_cost or bound > search_limit:
                break
            if score > scores[n]:
                continue
            if n in goals and score + goals[n] < best_cost:
                best_cost, best_node = score + goals[n], n
            for index in range(self.offsets[n],self.offsets[n+1]):
                neighbour = int(self.to_nodes[index])
                new_score = score + self.weights[index]
                if new_score < scores.get(neighbour,math.inf):
                    scores[neighbour] = new_score
                    parents[neighbour] = (n,index)
                    heappush(queue,(new_score+remaining(neighbour),new_score,neighbour))
        if not math.isfinite(best_cost):
            return None
        nodes = []
        estimated_sidewalk = bool(self.centerlines[a[0]] or self.centerlines[b[0]])
        # The first and last segments may be used for only a fraction of their length.
        final_edge_cost = best_cost if best_node is None else goals[best_node]
        uses_steps = bool(self.steps[b[0]] and final_edge_cost > 0)
        inferred_access = 0.
        while best_node is not None:
            nodes.append(best_node)
            if best_node not in parents:
                break
            best_node, arc = parents[best_node]
            edge = self.arc_edges[arc]
            estimated_sidewalk |= bool(self.centerlines[edge])
            uses_steps |= bool(self.steps[edge])
            if edge >= self.mapped_edge_count:
                inferred_access += self.length[edge]
        if nodes:
            uses_steps |= bool(self.steps[a[0]] and scores[nodes[-1]] > 0)
        path = [start, a[3] / SCALE + ORIGIN]
        path.extend(self.coords[n] for n in reversed(nodes))
        path.extend([b[3] / SCALE + ORIGIN,end])
        distance = float(np.sum(np.linalg.norm(np.diff(project(path),axis=0),axis=1)))
        if limit is not None and distance > limit:
            return None
        geometry = []
        for coord in path:
            latlon = [round(float(coord[0]),7), round(float(coord[1]),7)]
            if not geometry or geometry[-1] != latlon:
                geometry.append(latlon)
        return {'seconds':int(round((a[2]+best_cost+b[2])/SPEED)),
                'distance_m':int(round(distance)), 'geometry':geometry,
                'source':'OpenStreetMap pedestrian network',
                'uses_steps':uses_steps, 'inferred_crossing_access_m':round(inferred_access),
                'includes_arterial_without_mapped_sidewalk':estimated_sidewalk,
                'origin_snap_m':round(a[2],1), 'destination_snap_m':round(b[2],1)}
