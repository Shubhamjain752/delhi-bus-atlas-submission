# Did the bus start its trip?

On 24 November, a passenger leaving Rohini at 5:30 p.m. could be offered the 808STDOWN. Its timetable listed a 5:25 p.m. start at Rohini Sector 37 E Bus Depot. Atlas estimated it would reach the passenger's stop around 5:45 p.m. The October study below explains what the GPS archive can tell us about this service. Confirming that particular November bus requires evidence from 24 November.

## Where does a route start?

Atlas reads the ordered stops for each scheduled trip and takes the first one where ordinary passengers can board. One trip can start at a depot, another at a terminal or roadside stop. A shorter version of the same route can start somewhere else. The names and locations come from Delhi’s timetable. It does not identify a bus’s parking bay, or the time it leaves the garage before entering passenger service.

For a journey being planned now, Atlas looks for a fresh position from that particular scheduled run, on the correct service date, at a later stop. When it finds one, it can say the bus was already in service. Otherwise the departure stays unconfirmed. A bus elsewhere on the same route cannot confirm the named run.

## What October GPS can tell us

| October weekdays, 4–6 p.m. | What we found |
| --- | --- |
| **808STDOWN, Rohini Sector 37 E Bus Depot** | The timetable listed 198 starts. The monthly GPS records yielded no qualifying movement from the first stop to the second. How many of those departures ran is unknown. |
| **808STDOWN, Rohini Sector 20/21/22 Crossing toward Sector 21** | On 22 weekdays we could measure 64 gaps between recorded movements. The middle gap was **18 min 40 sec**. Nine in ten gaps were about **38 min 12 sec** or shorter. An untracked bus could have passed between sightings. |
| **764DOWN, Nehru Place Terminal toward Paras Cinema** | We counted 418 scheduled starts and 199 recorded movements from the first stop to the second. Across 177 gaps on 22 weekdays, the middle gap was **7 min 53 sec**. Matching each movement to a scheduled run requires its vehicle and trip identity. |

Atlas groups observations by route, the stop and its next stop in travel order, weekday or weekend, and two-hour Delhi time window. To count a start, the same bus and run must have reported near both stops in order. Ambiguous route starts and overlapping 60-metre stop areas are excluded. Gaps are measured between recorded visits to the first stop of a pair, within the same day and time window. Atlas reports a gap only after at least five usable intervals on three days. The median describes spacing between recorded buses. A passenger’s wait also depends on when they reach the stop.

The 31-day fit counted 163,185 qualifying origin movements on 1,663 route IDs. Platform gaps met the reporting threshold on 1,815 route IDs. The historical figures appear only when both the forecast date and the relevant travel time are on or after 1 November 2026, using the matching timetable version. To test the method on unseen October dates, we trained a separate version through 24 October. [Held-out results](departure-history-evaluation.md) · [November journey checks](departure-history-november.json)

### Matching a scheduled start by name

We checked all 31 days of October. A start counted when the same vehicle and run appeared within 45 metres of the first two stops in order, with an unambiguous match to a scheduled trip. Each position had to reach the receiver within 90 seconds. A bus could start up to five minutes early or an hour late. We counted scheduled starts only in five-minute periods when the archive contained some GPS. Coverage could still be missing at either stop.

GPS confirmed **18,525 of 2,401,829 eligible scheduled starts**, across 1,207 route IDs. Between midnight and 4 a.m. the count was 202 of 21,737. Those numbers measure how often these records could verify a run. The actual number of departures remains unknown.

| Weekdays, 4–6 p.m. | Scheduled starts | Starts linked to that scheduled run |
| --- | ---: | ---: |
| 808STDOWN, Rohini Sec 37 E Bus Depot to Rohini Sec 36 | 198 | 0 |
| 174DOWN, Kanjhawala Village (T) to Kanjhawala Chowk | 352 | 8 |
| 764DOWN, Nehru Place Terminal to Paras Cinema | 418 | 25 |

The 764DOWN illustrates why the two studies have different figures. The first study counted 199 movements along that stretch. The stricter study linked 25 starts to specific scheduled runs. Across Delhi in the final seven days, GPS confirmed 855 starts in groups that had recorded none in the first 24 days. A zero in October gives no basis to remove the November 808 from a passenger's choices.

The compressed file groups counts by route ID, first stop ID, next stop ID, weekday or weekend, and two-hour period starting at midnight in Delhi. In each group, `observed_dates` lists dates with eligible scheduled starts. A listed date can have zero confirmed departures. The summary also gives counts by day and the number of dates with confirmations for the three examples. [Monthly summary](scheduled-starts-october.json) · [Grouped counts (GZIP, 223 KB)](../data/from-2026-11/scheduled-start-check.json.gz)

Team IITBOYS calculated these statistics from the Delhi bus timetable v32 and the October 2026 DTC vehicle-position archive provided by Delhi Transport Stack and IIIT Delhi. The files contain grouped counts and coverage dates. Their SHA-256 checksum appears in the summary. [Data source](https://delhi.transportstack.in/data-services) · [Delhi Transport Stack data agreement](https://dts-backend.transportstack.in/api/document/download/1/422edd1d-4bfa-4a7d-843b-82d36e1eee6e)

## Rebuild

Use the project’s Python build requirements, the official v32 timetable ZIP and the original October GPS archive. First follow the [October extraction instructions](october-eta-candidate.md) to create the daily traversal labels and verified manifest. Set `$work` to that temporary folder and run these commands from the project root in PowerShell.

```powershell
python tools/fit_origin_service.py --labels "$work/labels" --work "$work"
python tools/evaluate_departure_history.py --labels "$work/labels" --training "$work/origin-service-holdout.json.gz" --origin-events "$work/origin-service-events.csv.gz" --output "$work/gap-evaluation.json"
python tools/fit_scheduled_starts.py --labels "$work/labels" --scratch "$work/scheduled-check"
```

The planner loads the compact October summary from `data/origin-service-history.json.gz`. Completed scheduled-run checks go to `data/from-2026-11/scheduled-start-check.json.gz`. The daily raw GPS files and temporary build data stay out of both artifacts.
