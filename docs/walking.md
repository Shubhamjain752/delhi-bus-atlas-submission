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

For a street-level pin, Atlas excludes the interior of mapped underground segments when attaching the endpoint. Stairs and underpasses can still be part of the route. The companion `data/walk-surface.json` is built from the same pinned OpenStreetMap extract as `walk.npz`. `data/walk-station-access.json` records the reviewed short pavement approaches at Rajiv Chowk Metro Gate 7. [See the station entrance, place pin and bus platform on the map](rajiv-chowk-walk.md)

The router projects the two pins onto nearby mapped pedestrian segments within 65 m. It checks the short approach lines against mapped walls, railways, waterways and major surface roads, then finds the fastest path through shared OSM nodes. A flyover and the road beneath it connect only where the map contains a shared node. The streets used by cars in one direction may permit pedestrians in both directions. The router respects explicit foot access, foot-specific direction and mapped barriers during graph construction.

Time uses 1.25 m/s on ordinary segments, a 1.5× time multiplier on steps and a 20-second allowance for a through passage at a mapped signalised pedestrian crossing. These are modelling assumptions. Each response includes the total distance, time, geometry, approach distances and a flag when the walk uses an arterial segment with an unconfirmed sidewalk.

The first and last short line are inferred connections. A shelter coordinate on a carriageway, an unmapped median, an indoor concourse, a locked gate or a missing bridge entrance can change the actual walk. Check pedestrian entrances, carriageway side and accessible routes on the ground before using a path as public navigation guidance. The model includes stairs and offers a general walking estimate.

No path is returned for a missing connection or an endpoint outside the snap limit. A `None` result reports what the available map can establish. Multiple bus stop records with the same name may refer to different boarding locations.

The delivered graph contains 1,155,725 nodes and 1,276,782 mapped segments. In the supplied GTFS v32 feed, 5,813 of 6,862 stops (84.7%) attach to at least one segment within the connector rules. Of these, 3,465 attach to the largest connected group when direction is ignored. An individual stop can attach successfully and still lack a route to another stop. For example, the graph estimates 237 m between Kashmere Gate Metro Gate 3 and Kashmere Gate Terminal (stop IDs 4232 and 4109) and 1,019 m from Nehru Place to Paras Cinema (2559 and 6054). It cannot connect stops 6026 and 4232 at Kashmere Gate. These are network estimates, with no on-site timing measurements.

Run the walking regression checks with `python -m unittest discover -s tests -p test_walking.py -v`. They cover divided-road crossings, barriers, directed foot access, partial-edge routing and published stops near Kashmere Gate and Nehru Place.

## Street estimate at Nehru Place

Nehru Place stops 2559 and 2563 sit on opposite sides of Lala Lajpat Rai Marg. The supplied walking graph cannot establish a pedestrian connection. For a rider considering a nearby bus, Atlas can show a separate estimate from the FOSSGIS walking router. Its mapped line follows the two carriageways and joins them at a surface road junction. The public map does not identify a continuous pavement or marked pedestrian crossing there.

The router returned 314.2 metres in each direction on 24 November 2026. Including the roughly one-metre and four-metre links between the stop coordinates and road lines gives about 319 metres, or 4 minutes 15 seconds at 4.5 km/h. Crossing delays are unknown. Atlas keeps this estimate outside its confirmed journey times. A rounded map pin can use it only within three metres of a reviewed stop, on the same side of the mapped road, with a connector clear of mapped barriers.

The evidence file includes both original requests, dates, response hashes, ordered OSM node IDs and the returned coordinates. Re-run the linked foot-profile requests to compare against the current map; a later map can give a different result. [Evidence and source queries](../data/walk-estimates.json) · [Mapped junction](https://www.openstreetmap.org/way/1541278191) · [Routing service](https://routing.openstreetmap.de/) · [OpenStreetMap data licence](https://www.openstreetmap.org/copyright)

## Shivaji Stadium to Rajiv Chowk

The destination makes a difference at this metro station. From Shivaji Stadium bus platform 365, the earlier planner took a passenger through Gate 6 and the underground concourse to reach a map pin above it. For a passenger asking for Gate 7, it approached the gate from inside the station. OpenStreetMap shows stairs there, but has a gap between the top of the stairs and the pavement. [Gate 7 on OpenStreetMap](https://www.openstreetmap.org/node/928610588) · [Underground passage](https://www.openstreetmap.org/way/582355768)

Atlas now approaches Gate 7 from the street. It follows the terminal lane to the signal, turns onto Radial Road 2, uses its marked crossing and allows 26 metres from the near curb to the top of the stairs. That gives 332 metres and 4 min 50 sec. The last 26 metres are an estimate. We checked the two short approaches from the gate to its block's mapped crossings for intervening carriageways, walls, fences and surface buildings. Nobody surveyed the pavement or checked the gate's opening hours. [Radial Road 2 crossing](https://www.openstreetmap.org/way/1234918802) · [Inner Circle crossing](https://www.openstreetmap.org/way/1234918803) · [Approach coordinates and source](../data/walk-station-access.json)

Entering the pin at 28.632986, 77.219374 still means walking to that point in Central Park. Atlas estimates 653 metres and 9 min 7 sec, including about 23 metres from the mapped park path to the pin. It uses the terminal approach and mapped surface crossings. Google Maps returned about 300 metres for the named station and about 500 metres for the coordinate. Walking these paths would establish which public approaches are usable on the day.

The map builder records below-ground segments and street-level subway entrances in `data/walk-surface.json`. When attaching a coordinate to the map, Atlas excludes the interior of a below-ground passage. A pin on a mapped surface entrance can use the stairs there. Underground passages remain available as intermediate parts of a connected walk. The access file belongs to the same dated extract and edge order as `data/walk.npz`.
