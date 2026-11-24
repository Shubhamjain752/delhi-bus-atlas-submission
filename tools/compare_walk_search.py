"""Compare the walking search with Dijkstra on every published journey-access walk.

Run with Python 3.12 from the repository root:
    python tools/compare_walk_search.py

An expansion counts one examination of a graph node's outgoing connections.
The saved examples are a targeted sample, and the comparison measures search
work and agreement of predicted walking times, not actual walking speed.
"""

import json
import math
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from transit import walking


class CountAdjacencyScans:
    """Each outgoing-edge scan reads its start and end from the offset array."""

    def __init__(self, offsets):
        self.offsets = offsets
        self.reads = 0

    def __getitem__(self, index):
        self.reads += 1
        return self.offsets[index]


def measure(graph, start, end, search_math):
    graph._snap.cache_clear()
    counter = CountAdjacencyScans(graph.offsets)
    with patch.object(graph, "offsets", counter), patch.object(walking, "math", search_math):
        # Bypass the saved route cache, retaining normal snapping and edge weights.
        route = graph._route(start, end, None)
    if counter.reads % 2:
        raise AssertionError("The two-offset adjacency scan in Walking._route has changed")
    seconds = None if route is None else route["seconds"]
    return seconds, counter.reads // 2


def compare():
    data = ROOT / "data"
    graph = walking.Walking(data, use_crossing_access=True)
    cases = json.loads((data / "journey-access-check.json").read_bytes())["walks"]
    # The only hypot call in the walking module supplies the A* lower bound.
    # A zero lower bound orders the same search by cost from the start (Dijkstra).
    dijkstra_math = SimpleNamespace(**{**vars(math), "hypot": lambda x, y: 0.0})
    rows = []
    for case in cases:
        start = tuple(case["origin"])
        end = tuple(case["destination"])
        astar_time, astar_nodes = measure(graph, start, end, math)
        dijkstra_time, dijkstra_nodes = measure(graph, start, end, dijkstra_math)
        rows.append({
            "key": case["key"],
            "astar_seconds": astar_time,
            "dijkstra_seconds": dijkstra_time,
            "astar_expansions": astar_nodes,
            "dijkstra_expansions": dijkstra_nodes,
        })
    reached = [r for r in rows if r["astar_seconds"] is not None and r["dijkstra_seconds"] is not None]
    return {
        "walks": len(rows),
        "both_returned_routes": len(reached),
        "equal_walking_times_on_returned_routes": sum(r["astar_seconds"] == r["dijkstra_seconds"] for r in reached),
        "both_unavailable": sum(r["astar_seconds"] is None and r["dijkstra_seconds"] is None for r in rows),
        "astar_expansions_on_returned_routes": sum(r["astar_expansions"] for r in reached),
        "dijkstra_expansions_on_returned_routes": sum(r["dijkstra_expansions"] for r in reached),
        "details": rows,
    }


if __name__ == "__main__":
    print(json.dumps(compare(), indent=2))
