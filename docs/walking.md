# Delhi pedestrian routing

The journey API uses `data/walk.npz` locally. Its network comes from the 1 November 2026 Northern Zone extract and covers the Delhi NCT plus portions of adjoining NCR within latitude 28.25–28.96 and longitude 76.78–77.55. Coverage depends on mapped pedestrian connections. The graph records a single street snapshot for the October GPS replay.

**Map data © OpenStreetMap contributors, distributed under the Open Database License (ODbL) 1.0. Extract by Geofabrik.** The derived pedestrian database is available under the same license. The input URL, checksum, bounds and counts are recorded in `data/walk-meta.json`. [OpenStreetMap attribution and licensing](https://www.openstreetmap.org/copyright) · [Geofabrik Northern Zone](https://download.geofabrik.de/asia/india/northern-zone.html)

## Build

Python 3.12 was used to build the extract. Install the build dependency into the project Python environment and run the builder from the repository root. NumPy and SciPy are runtime dependencies. The builder verifies the pinned source against Geofabrik's published MD5 hash before use.

```powershell
python -m pip install numpy scipy osmium==4.3.2
python tools/build_walk.py --output data
```

Use `--pbf "path/to/northern-zone-261101.osm.pbf"` to reuse the download. For a newer extract, supply its new `--source-url` and matching `--md5`. The generated NPZ contains numeric arrays and loads without pickle.

## Estimating a walk

The router projects the two pins onto nearby mapped pedestrian segments within 65 m. It checks the short approach lines against mapped walls, railways, waterways and major surface roads, then finds the fastest path through shared OSM nodes. A flyover and the road beneath it connect only where the map contains a shared node. The streets used by cars in one direction may permit pedestrians in both directions. The router respects explicit foot access, foot-specific direction and mapped barriers during graph construction.

Time uses 1.25 m/s on ordinary segments, a 1.5× time multiplier on steps and a 20-second allowance for a through passage at a mapped signalised pedestrian crossing. These are modelling assumptions. Each response includes the total distance, time, geometry, approach distances and a flag when the walk uses an arterial segment with an unconfirmed sidewalk.

The first and last short line are inferred connections. A shelter coordinate on a carriageway, an unmapped median, an indoor concourse, a locked gate or a missing bridge entrance can change the actual walk. Check pedestrian entrances, carriageway side and accessible routes on the ground before using a path as public navigation guidance. The model includes stairs and offers a general walking estimate.

No path is returned for a missing connection or an endpoint outside the snap limit. A `None` result reports what the available map can establish. Multiple bus stop records with the same name may refer to different boarding locations.

The delivered graph contains 1,155,725 nodes and 1,276,782 mapped segments. In the supplied GTFS v32 feed, 5,813 of 6,862 stops (84.7%) attach to at least one segment within the connector rules. Of these, 3,465 attach to the largest connected group when direction is ignored. An individual stop can attach successfully and still lack a route to another stop. For example, the graph estimates 237 m between Kashmere Gate Metro Gate 3 and Kashmere Gate Terminal (stop IDs 4232 and 4109) and 1,019 m from Nehru Place to Paras Cinema (2559 and 6054). It cannot connect stops 6026 and 4232 at Kashmere Gate. These are network estimates, with no on-site timing measurements.

Run the walking regression checks with `python -m unittest discover -s tests -p test_walking.py -v`. They cover divided-road crossings, barriers, directed foot access, partial-edge routing and published stops near Kashmere Gate and Nehru Place.
