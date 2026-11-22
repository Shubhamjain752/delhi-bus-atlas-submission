"""Create a lightweight, offline cartographic layer from the routing OSM extract."""
import argparse
import gzip
import json
from pathlib import Path

import osmium
from shapely.geometry import LineString, box, mapping


def build(source, target):
    area = box(76.78, 28.25, 77.55, 28.96)
    features = []
    for way in osmium.FileProcessor(str(source)).with_locations("flex_mem").with_filter(osmium.filter.EntityFilter(osmium.osm.WAY)):
        road = way.tags.get("highway")
        waterway = way.tags.get("waterway")
        if road in {"motorway", "trunk", "primary", "secondary", "tertiary"}:
            kind = "arterial" if road in {"motorway", "trunk", "primary"} else "street"
        elif waterway in {"river", "canal"}:
            kind = "river"
        else:
            continue
        coords = [(n.lon, n.lat) for n in way.nodes if n.location.valid()]
        if len(coords) < 2 or not any(76.76 < x < 77.57 and 28.23 < y < 28.98 for x, y in coords):
            continue
        geom = LineString(coords).intersection(area).simplify(0.00016)
        if geom.geom_type not in {"LineString", "MultiLineString"} or geom.is_empty:
            continue
        geometry = mapping(geom)
        def rounded(points):
            if isinstance(points[0], (int, float)):
                return [round(p, 5) for p in points]
            return [rounded(point) for point in points]
        features.append({"type":"Feature", "properties":{"kind":kind}, "geometry":{
            "type":geometry["type"], "coordinates":rounded(geometry["coordinates"])}})
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as output:
        json.dump({"type":"FeatureCollection", "features":features}, output, separators=(",", ":"))
    tmp.replace(target)
    print(f"{len(features):,} mapped ways -> {target} ({target.stat().st_size:,} bytes)")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("pbf", type=Path)
    p.add_argument("--output", type=Path, default=Path("data/map-context.geojson.gz"))
    args = p.parse_args()
    build(args.pbf, args.output)
