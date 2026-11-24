# Can the timetable be trusted at the start of a route?

An evening passenger in Rohini had 13 minutes to walk to the pickup stop. Atlas estimated an 808STDOWN would pass at 5:45 p.m. To check that estimate, we looked for two different things. Had a vehicle on this particular service already been seen moving? And on other weekdays, how much time had passed between recorded 808 buses at this stop?

The timetable identifies the first place where a passenger can board each scheduled trip. For this trip it calls that stop Rohini Sector 37 E Bus Depot. Other services start at ordinary stops or terminals. GPS can show a bus progressing from the first public stop to the next. It cannot see every departure from the gates of a depot.

## What the passenger could know in November

October’s timetable listed **198 weekday starts** on this 808 route between 4 and 6 p.m. We found **zero** movements meeting the GPS test at the origin. At the passenger’s stop farther along the route, GPS did supply a useful history: **64 gaps over 22 weekdays**. The middle gap was **18 minutes 40 seconds**. Nine out of ten were about **38 minutes 12 seconds** or less. Missing vehicles can lengthen those gaps.

On 24 November the recorder received 41 position reports before the passenger’s 5:30 p.m. search. None identified the scheduled 5:25 run. An 808 appeared at the pickup around 6:29 p.m. with a different start time. The new evidence can explain the uncertainty about boarding; it cannot establish whether the 5:25 run departed, how long this passenger would wait, or when they arrived.

We repeated that check for the first ordinary choice on seven November journeys, plus the conditional 502UP at Adhchini. Three of the seven ordinary choices had enough October GPS at their boarding platforms to show a historical gap. None of the seven archives had recorded a uniquely matched vehicle at both its first stop and its next stop by the search time. Some collection windows began after the scheduled bus had started. A recent match downstream is a separate source of evidence. [Results for all seven departures](departure-history-november.json)

## Does the history hold up on another week?

We learned the intervals on 1–24 October and tested them on 25–31 October. We compared Thursday with weekday history, Sunday with weekend history, and used the same two-hour Delhi time window in training and testing. Each movement had to pass from one stop to its next on the same route. Overlapping stop areas and gaps touching two observations at the same second were excluded. We required five historical gaps across at least three dates before using a typical interval.

At route origins, 12,202 held-out gaps had both timetable and historical comparisons. The average error in the gap between two recorded movements was **16 min 26 sec with timetable spacing** and **14 min 36 sec with historical spacing**. Across all eligible platforms, 3,471,818 of 3,681,291 gaps had enough history. A future gap differed from its historical median by 12 min 56 sec on average. About 86.5% fell below or at the historical 90th percentile, including 85.8% of 13,134 overnight gaps.

We separately asked how long someone arriving at a route origin every quarter-hour would wait until the next **recorded** movement. We scored the same clocks for three methods. The last column gives longer gaps more weight, since a randomly arriving passenger is more likely to encounter one.

| Held-out clocks | Clocks scored | Timetable, average error | Half the historical median gap | History weighted by gap length |
| --- | ---: | ---: | ---: | ---: |
| All hours | 17,930 | 17 min 35 sec | 14 min 37 sec | 13 min 53 sec |
| Weekdays | 13,355 | 17 min 57 sec | 14 min 55 sec | 14 min 7 sec |
| Weekends | 4,575 | 16 min 30 sec | 13 min 45 sec | 13 min 10 sec |
| Midnight to 4 a.m. | 203 | 13 min 10 sec | 10 min 27 sec | 10 min 52 sec |

Coverage limits this comparison. We tried 847,280 quarter-hour clocks in eligible scheduled bands represented in training. Only 25,260 fell between two recorded movements in the same two-hour window. Requiring a subsequent scheduled departure within the window and enough training history left 17,930 comparable clocks (2.1%). Long gaps crossing a window boundary are excluded. Earlier buses could have passed without reporting. These results justify showing historical spacing as context. They do not establish an improvement in passenger wait or door-to-door ETA.

Missing GPS also proved a poor reason to remove a bus from the timetable. In the test week, we saw 1,190 start movements at places and times where the first 24 days had supplied none. The public planner keeps these services available. [Results and counts](departure-history-evaluation.json)

### Reproduce the tests

Run the [October fitting script](../tools/fit_origin_service.py) with the original 31 daily traversal files. Give its pre-25 October fit and origin-event export to the [evaluation script](../tools/evaluate_departure_history.py) using `--training`, `--origin-events`, `--labels` and `--output`. The [November check](../tools/check_november_origin_history.py) takes the 31-day fit, the local dated receipt folders and the three overnight packet archives. Counts in both reports refer to recorded movements. They cannot identify the number of cancellations.
