# Where does the walk to Rajiv Chowk end?

A passenger leaving the 943 at Shivaji Stadium can walk to a metro entrance or continue to the station’s place pin inside Central Park. The Google transit screenshot from 24 November showed a five-minute, 300 m walk along Connaught Circle and Radial Road 2. The blue walking dots end at the southwest side of Inner Circle. The red station marker is farther away. The approach matches Gate 7 in A Block. The screenshot does not identify a staircase by number. [Delhi Metro station information](https://delhimetrorail.com/station/RCK) · [Gate 7 on OpenStreetMap](https://www.openstreetmap.org/node/928610588)

We repeated Google’s walking search on 25 November from the exact coordinates of bus platform 365 (28.632300, 77.216500). Google gave 300 m and four minutes to the **named station**, 270 m and four minutes to **Gate 7’s coordinates**, and 500 m and seven minutes to the **place pin** (28.632986, 77.219374). Only the last search continued past the southwest approach towards the centre. [Named station directions](https://www.google.com/maps/dir/?api=1&origin=28.6323,77.2165&destination=Rajiv+Chowk&destination_place_id=ChIJr_S96zf9DDkRhEvfzvzu5ys&travelmode=walking) · [Directions to the literal coordinates](https://www.google.com/maps/dir/?api=1&origin=28.6323,77.2165&destination=28.632986,77.219374&travelmode=walking)

## What the walking trace showed

The earlier route to the pin went down the stairs at Gate 6 and ended on the underground concourse. The walking graph had attached the map pin to the closest line in two dimensions. When we explicitly requested Gate 7, the graph could reach its staircase only from inside the station. Its mapped pavement connection was missing.

Atlas now treats ordinary map coordinates as street-level endpoints. Underground passages remain available through mapped entrances. At Gate 7 we reviewed two short approaches from the entrance to the near curbs of existing marked crossings on Radial Road 2 and Inner Circle. The route from Shivaji Stadium uses an estimated 26 m of approach to the gate. Nobody has surveyed it. [Entrance and crossing evidence](../data/walk-station-access.json)

| From Shivaji Stadium platform 365 | Previous Atlas walk | Corrected Atlas walk | Google walking search, 25 November |
|---|---:|---:|---:|
| Metro Gate 7 coordinates | 807 m · 11 min 26 sec | 332 m · 4 min 50 sec | 270 m · 4 min |
| Place pin in Central Park | 677 m · 9 min 40 sec | 653 m · 9 min 7 sec | 500 m · 7 min |

The gate route follows the mapped terminal lane to the signal at Outer Circle and then follows Radial Road 2. The park route continues across Inner Circle using a marked crossing. The original bus-stop coordinate lies near the middle of a four-lane roundabout. Atlas allows about 137 m to reach the signal by the terminal lane. Google shows a more direct approach. The precise boarding kerb and walking conditions still need a field check. [View the Atlas traces on a map](rajiv-chowk-walk.geojson) · [Try Gate 7](https://iitboys-delhi-bus-atlas.vercel.app/?example=vivek-gate-7) · [Try the place pin](https://iitboys-delhi-bus-atlas.vercel.app/?example=vivek-inner-circle)

## How the timetable and GPS contributed

The 10:57 a.m. forecast starts with the buses scheduled to run that Tuesday and the order in which they serve their stops. For the 243DOWN and 943UP connection at Nirman Vihar, Atlas used October weekday GPS history to estimate the 16 stretches on the first bus and 13 on the second. It added the walks, the time before the first bus and the wait for the connection. A published running time is available as a fallback where history is thin. Our recorder started that evening. Whether the two buses actually reached the stops at those times is unknown.

## A shorter walk for a similar arrival

For the shared park pin, taking the 243DOWN and changing to the 943UP gives an estimated arrival of 12:19:49 p.m. Changing to the 73UP gets there at 12:19:38 p.m. Walking almost five extra minutes saves just 11 seconds overall, so Recommended puts the 943UP option higher. A longer walk can still rank first when it cuts at least 10% off the whole trip. [Ranking rules](api.md)

We reran 22 Delhi searches, including three after midnight. The first choice stayed the same in all 22. The earliest arrival offered stayed the same in 21 searches and improved by 55 seconds in one. Five lists changed order or offered different alternatives. These comparisons measure changes in the journeys Atlas offers. We have no recorded bus positions for the Vivek Vihar trip that morning.
