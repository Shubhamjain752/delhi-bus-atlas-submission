# Finding gaps between metro gates and crossings

Could a computer find the missing link at Rajiv Chowk Gate 7 without knowing its name? We checked every street-level metro entrance indexed in the Atlas walking graph. The scan found both approaches we had previously reviewed at Gate 7. It also found two other gates worth checking in person. This is an offline audit. Running it writes evidence files and makes no changes to passenger routes.

## What the scan found

Atlas has 149 indexed street-level entrances across its Delhi-area graph. Ten had walking links leading only below ground in the original map. Four of those ten were within 40 metres of an explicitly marked crossing in the newer Delhi map.

| Metro entrance | What lies between the gate and the crossing |
|---|---|
| Rajiv Chowk Gate 5 | Both possible links meet an obstacle. One intersects a road, the other a restricted service way. |
| Rajiv Chowk Gate 7 | The scan recovered the two previously reviewed approaches, 12 m and 26 m. Going directly to the opposite ends of those crossings would cut across a road. |
| Rajiv Chowk Gate 8 | The nearer end of the crossing is 10 m away. The map shows no obstacle along that line. The farther end is across a road. |
| New Delhi Metro Gate 3 | One end of a marked crossing is 27 m away. The map shows no obstacle along that line. |

We found no mapped public pedestrian area covering the entire gap for either new candidate. A fence or a change of level could be missing from the map. Both gates remain leads for review. [Distances and crossing IDs](gate-proximity.json) · [Obstacle checks for each approach](approach-checks.json)

## How we checked

1. Read the original walking graph before the reviewed Gate 7 links were added. Flag entrance nodes attached only to underground paths. A tag giving the gate name or number is optional.
2. Read public, street-level metro entrances and two-way marked pedestrian crossings from the newer OpenStreetMap extract. Match gate coordinates to the graph at the precision used in OSM. Measure the straight-line distance to the ends of each crossing. The 40 m search radius is Atlas's existing limit for a short missing approach.
3. For each candidate, test the straight line from the gate to each end of the crossing. Check for mapped roads, railways, water, buildings, barriers and restricted access. Treat contact with an obstacle conservatively. Look separately for a mapped pedestrian area enclosing the whole line.

The newer National Capital Territory extract had 471 tagged entrance points. Of these, 447 met the street-level and access filters, 129 matched an indexed entrance in the release, and 18 were near a marked crossing. Thirteen of those 18 matched the release and four also had the underground-only pattern. The graph and newer extract cover different areas and dates. These figures describe the files we checked. They cannot tell us how many gates in Delhi have missing pavements or whether a passenger can use a particular entrance.

Three area records in the extract had no usable outline. The obstacle report lists their map IDs. We could not tell where they fell.

## Reproduce the audit

From the repository root with Python 3.12, install the build dependencies. Use the National Capital Territory PBF whose replication time is **23 November 2026, 00:26:51 UTC**. The download URL is refreshed over time. A newer download can be analysed, but only the checksum below identifies the file used for these figures. The release graph comes from the 1 November Geofabrik Northern Zone extract. [NCT extract download](https://download.openstreetmap.fr/extracts/asia/india/national_capital_territory_of_delhi.osm.pbf) · [Graph metadata](../../data/walk-meta.json)

```sh
python -m pip install -r requirements-build.txt
python tools/audit_gate_crossings.py --pbf national_capital_territory_of_delhi.osm.pbf --data data --output gate-audit-results --expect-sha256 d94b858e16be90416da4cd16cc21be07aa5dd53b167e909c813ee546c92ecb1a
```

The command produces the two JSON files linked above. To check a different date, omit the checksum or supply the digest for that extract. The reports record the checksum and map timestamp actually used. [Audit code](../../tools/audit_gate_crossings.py)

Map data © OpenStreetMap contributors, available under the Open Database License. [OpenStreetMap attribution and licence](https://www.openstreetmap.org/copyright)
