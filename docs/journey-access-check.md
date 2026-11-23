# Checking Delhi journeys after the walking and direct-bus changes

We compared the previously prepared submission with the revised planner using the same timetable, walking limits, public stops, departure times and GPS receipt clocks. For each request we inspected the first three ordinary journeys and recorded any separate conditional boarding offer. A different predicted arrival tells us that the software changed its advice. An actual trip needs timed evidence.

The check covers 29 requests. Four are the recorded AIIMS, Punjabi Bagh, Kashmere Gate and Rohini journeys. Three use the overnight 012(NS)UP, 502UP and 604UP vehicle recordings. Eleven use platform pairs on different Delhi corridors at 08:30, 13:00 or 17:30. Another eleven use nearby street pins fixed before the comparison. The 27 walking checks comprise five previously investigated access or transfer paths and both ends of those eleven corridors. The corridors and clock bands are a targeted regression set. They do not estimate citywide accuracy.

## What changed

| Result on the same inputs | Submitted planner | Revised planner |
| --- | ---: | ---: |
| Queries with an ordinary bus journey | 24 of 29 | 24 of 29 |
| Queries showing a direct bus among the first three | 21 of 29 | 22 of 29 |
| First AIIMS–Lajpat Nagar choice at 15:00 | 623UP → 724AUP, 24m 29s | 711UP direct, 19m 16s |
| First-choice time on the other 23 served requests | Reference | Identical |

Ten of the 24 revised first choices now list more lines that call at the same boarding and alighting platforms. At AIIMS the list includes 479UP, so a rider can consider that line if it comes first. From the ITO AGCR pin to the Jagatpuri F Block pin the revised top three are all direct (317UP, 720DOWN and 623DOWN). The previous top three included a change from 85DOWN to 317UP.

Four of the 27 checked walking links became shorter. Fifteen retained the same mapped distance. Eight still had no connected walk. The distances include estimated approaches to the crossing. Nobody field-timed them.

| Walking link | Previous | Revised |
| --- | ---: | ---: |
| AIIMS pin → Google’s boarding pole | 11,052 m | 369 m |
| AIIMS pin → South Extension platform 2840 | 10,707 m | 788 m |
| Between South Extension platforms 2828 and 2840 | 10,249 m | 1,263 m |
| South Extension Petrol Pump → nearby test street pin on the 507UP corridor | 10,579 m | 2,134 m |

The last two distances still exceed the planner’s 1,100 m access limit. Five street-pin journeys still received no complete ordinary itinerary. Around Ras Vihar on the 534UP corridor, for example, the revised result can identify routes operating from a nearby pole while the passenger’s path to it remains unverified.

The three overnight GPS checks did not change. At Vasant Kunj, the predicted 604UP arrival came **2 seconds after** the bus was first seen at the final stop. At MMTC, the **conditional** 502UP estimate came **53 seconds before** the final-stop observation; the later fallback remained available. At Sarai Peepal Thala, 012(NS)UP remained **8 minutes 3 seconds late** against the observed stop passage. These are checks on particular vehicles. [All 29 before-and-after results and the 27 walks](../data/journey-access-check.json)

## Would a minute have caught the 433?

The Sunday 433 appeared near the boarding point between 15:01:16 and 15:01:49. At the pace used in the saved walking scenario the passenger would be ready at 15:04. Even the later GPS observation is 2 minutes 11 seconds before that. Google allowed five minutes to reach the stop, which increases the difference to 3 minutes 11 seconds. A one-minute margin cannot bridge either gap under this scoring rule. GPS did not record door state.

We also replayed forecasts that were **31–60 seconds** before platform readiness, using three days of October bus GPS and the submitted boarding policy. Each replay used only GPS information received by the decision time. Later fixes supplied the outcome. Candidates were deliberately chosen near the cut-off. A forecast was reconstructed from historical positions, and the rider’s ready time was simulated.

| Peak-hour date | Opportunities | Approaching / already gone / unresolved with blanket grace | Conditional itineraries the planner actually offered |
| --- | ---: | ---: | --- |
| 22 October | 195 | 9 / 115 / 71 | 1 already gone |
| 26 October | 191 | 5 / 108 / 78 | 2 already gone, 5 unresolved |
| 29 October | 164 | 5 / 86 / 73 | 1 unresolved |
| **All three dates** | **550** | **19 / 309 / 222** | **3 already gone, 6 unresolved** |

There were 40 route IDs across the three days. A timetable trip could be matched uniquely in 226 cases. The production rule exposed nine secondary itineraries at the requested pair of stops. No later GPS outcome confirmed an approach among those nine. With a 30-second cut-off none of these 31–60-second cases would qualify. Drivers’ door opening and actual passenger boarding remain unknown. [One-minute replay totals by date](../data/boarding-minute-check.json)

## Reproduce

For the full GPS replay, use the judge-provided data or your authorized local copies of the original receipts and previous planner. The public checkout includes the regression scripts and results.

Use Python 3.12 and the build dependencies. The two commands below take the root of the previously extracted working prototype and the updated checkout. The original submission ZIP had SHA-256 `db2d9fe4f10c4adf84d019724122a7e040a2c306fc592b0adc82e3989765b443`.

```sh
python tools/check_journey_access.py PATH_TO_PREVIOUS_PROTOTYPE baseline.json
python tools/check_journey_access.py . data/journey-access-check.json --before baseline.json
```

To reproduce the margin check, point the first argument at the *previous* project containing its original routing policy. The second directory must contain the extracted 22, 26 and 29 October GPS databases. No live feed or Google API key is needed.

```sh
python tools/check_boarding_minute.py PATH_TO_PREVIOUS_PROTOTYPE PATH_TO_GPS data/boarding-minute-check.json
```
