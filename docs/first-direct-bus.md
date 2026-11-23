# Which direct bus came first?

This check follows a passenger standing at a Delhi bus stop. Several lines can take them to the same next stop. We compare the line at the top of the old and revised planners with the first direct bus we could verify in the vehicle GPS.

## Results

Both planners returned a direct bus for every one of the 1,728 queries. GPS identified a clear first line at the boarding stop in 655 cases. The old planner picked that line 107 times (16.3%). With GPS informing the ranking it picked it 114 times (17.4%). Ten choices improved, three regressed and 21 switched between two incorrect lines. Another 45 changed choices had no clear boarding winner.

| Departure time in Delhi | Queries | Clear first line at boarding | Old choice correct | Revised choice correct |
| --- | ---: | ---: | ---: | ---: |
| After midnight, 00:10–04:10 | 360 | 63 | 17 | 18 |
| Peak, 08:10–10:10 and 17:10–19:10 | 432 | 192 | 30 | 33 |
| Remaining hours | 936 | 400 | 60 | 63 |
| **All hours** | **1,728** | **655** | **107** | **114** |

The first line to reach the **exit** could be verified 605 times. The top choice matched it 106 times before and 113 after. After midnight both versions matched 16 of 59. The revised first choice explicitly used an upstream GPS sighting in 55 of the 1,728 queries. The full replay covered 1,137 directed stop pairs at 1,080 boarding platforms across 82 geographic cells. The pairs could be served by 2,205 official route IDs, and GPS verified 1,392 different lines at both ends of at least one pair.

- **Corrected choice.** On 22 October at 10:10, from Jhandewalan to Paharganj Police Station, 954DOWN was the first verified line. The new planner replaced 775EDOWN with 954DOWN.
- **New error.** On 24 October at 15:10, from Kasturi Ram School to Babu Jag Jeevan Ram Colony, the earlier planner correctly picked 179DOWN. The revised planner picked 172STL-NDEVUP.
- **Gaps in the GPS.** Among the 1,073 unscored boarding choices, 363 had no fully traced line, 299 had only one, 225 had too little time between rivals and 186 had an earlier rival whose exit was missing. Even among the 655 scored cases, 340 revised picks lacked a complete trace. In the smaller group where both planners' chosen lines were tracked end to end, the result was 106 correct out of 304 before and 108 after.

## How we checked

- We used the official timetable to choose stop pairs **before** inspecting the GPS. At least two distinct lines had to be scheduled to leave for the same next stop within half an hour. At ten minutes past every hour, we picked up to 12 pairs in different areas of Delhi. Six slots favour busy links. A reproducible draw fills the other six. The draw changes with the hour and date.
- The six GPS dates were 22, 23, 24, 25, 26 and 29 October 2026. They include a Saturday, a Sunday and all 24 hours of the clock. The weekday travel-time model was trained on 12–13 October, and the weekend model on 10–11 October.
- For each query, both planners received the same four minutes of vehicle position messages leading up to the forecast. Each message was replayed at the time the archive received it. The software independently checks the age of the vehicle's GPS clock. We supplied the trip identifiers exactly as recorded. The databases have no original provider predictions or explicit service date. The software resolves an unambiguous service date where it can. A live deployment may have a longer traffic history.
- We ran the production journey search twice from the **same boarding platform to the same exit platform**, with transfers disabled. Both runs kept the same bus timetable, historical travel times and recently observed traffic. Only the new use of upstream GPS to adjust a particular bus's times differed. First choice means the line on the top-ranked journey. The planner ranks by predicted arrival at the destination. A bus can board later and arrive first.
- To check the outcome, we required a GPS reading within 45 metres of each pole, in order, on the same vehicle, trip and route. We rejected ambiguous nearby stops, fixes over 90 seconds old and buses already at the boarding pole before the query. The boarding window was 30 minutes, with up to 10 more minutes to witness the exit. At least two different lines had to complete the pair. Rivals within 45 seconds of the leading observation or an earlier rival without an exit observation make the winner uncertain.

## What the numbers can tell us

A correct result means the planner showed the first **verifiable** line. An earlier untracked bus is always possible. These are journeys between bus stops. GPS does not tell us whether the doors opened or whether a passenger boarded. The panel spans Delhi and includes night service, but its selection favours places offering a choice, and every test passenger is already at the stop.

## Reproduce the comparison

Install the build dependencies from the project root. Unpack the specified daily GPS databases into one or more folders. Start with a new scratch directory. The script reads the databases without changing them. Times use India Standard Time.

```sh
python tools/check_first_direct_bus.py prepare --databases PATH_TO_DAILY_DATABASES --scratch SCRATCH
python tools/check_first_direct_bus.py score --scratch SCRATCH
python tools/check_first_direct_bus.py report --scratch SCRATCH --output data/first-direct-bus.json
```

The default panel uses 12 geographically separated pairs per hour when available. A fresh run with `--pairs-per-hour 0` at the prepare step evaluates all eligible pairs. Scratch files contain vehicle identifiers and belong on the research machine. [Aggregate results](../data/first-direct-bus.json)
