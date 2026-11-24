# Testing a month of travel times

The first Atlas prototype learned travel times from two weekdays and two weekend days. We tested whether more GPS history improved its predictions, then trained the version for November journeys on all 31 days of October.

## What improved

**Average next-stop error fell from 51 seconds to 34 seconds.** A model trained on 1–24 October and the original four-day model predicted the same 5,941,620 bus movements across 2,113 route IDs on 25–31 October. Both used history alone. The timetable averaged 55 seconds of error on these movements.

| Movements in the seven-day test | Observations | Four days of history, average error | 24 days of history, average error |
| --- | ---: | ---: | ---: |
| All movements | 5,941,620 | 50.96 s | 34.13 s |
| Weekday | 4,128,661 | 50.69 s | 33.47 s |
| Weekend | 1,812,959 | 51.60 s | 35.64 s |
| Midnight to 4:59 a.m. | 61,238 | 47.49 s | 18.18 s |

The midnight row overlaps the weekday and weekend rows. For this comparison, both models used the Delhi date of the upstream GPS reading. An overnight bus in the live planner can belong to the preceding service date.

The fraction of predictions within one minute rose from 77.4% to 87.4%. Nine out of ten predictions were within 74 seconds with the candidate, compared with 112 seconds previously. Average error fell on 2,074 route IDs, rose on 32 and tied on seven. The candidate tended to predict rides about 14 seconds shorter than observed, compared with 12 seconds for the existing model.

We also built a separate profile on all 31 dates for future journeys. In four offline November searches it returned direct or connecting options and the walking, waiting and riding times added up. On the 5:30 p.m. Rohini example it suggested the 808STDOWN and 174DOWN, arriving around 6:46 p.m. The forecast actually saved before departure used the earlier model and predicted 6:35 p.m. On the overnight 502UP example, supplying the archived operator updates preserved the conditional 1:19:42 a.m. arrival with either model. The forecast for the fallback bus changed. A better estimate of time on the bus can change which departures the planner thinks a passenger can catch.

[Measured results](../data/october-eta-comparison.json) · [Read the checks against Google Maps and observed buses](https://github.com/Shubhamjain752/delhi-bus-atlas-submission/blob/main/submission/05-validation.pdf)

## Comparison fixed before scoring

- Use 1–24 October for the candidate fit. Keep observations from 25–31 October out of that fit. The seven test dates include five weekdays and two weekend dates.
- Run the published weekday and weekend profiles and the candidate on the same observed, directed movements between consecutive stops. Include all usable route IDs and hours in the archive, including after midnight. Report which route IDs, hours or dates supplied no usable observations.
- Give each profile the same timetable duration. Compare the historical estimates before adding any live correction. The result measures the benefit of additional history. First-bus waiting, transfers and a passenger’s door-to-door time require separate tests.
- For each usable movement, score the predicted seconds against elapsed time from GPS. Report the average absolute error, the error within which 90% of observations fall, signed bias and observation count. Show weekdays, weekends and overnight separately, as well as the routes or stop pairs on which only the new fit can use history.
- Keep the published 15 and 17 October results with the four-day model. Those dates are part of the candidate’s 1–24 October fit. Only the later test dates provide fresh evidence for this comparison.

After evaluation we fitted the same method on all 31 dates. Those weekday and weekend files ship with the public planner for journeys from 1 November onward. October replays use the earlier profiles so they cannot see later GPS. The 34-second result belongs to the seven days set aside in the October test. The saved Rohini forecasts still show what each planner said before departure. [Inspect the running model](https://iitboys-delhi-bus-atlas.vercel.app/status)

## Reproduce the experiment

To repeat the four November daytime searches with each model, run `python tools/replay_november_trips.py` after setting up the planner. With the original November recordings, `--witness-dir PATH` also checks the recorded boarding and exit events, and `--overnight` replays the three after-midnight searches from the local 21 November capture. The dated results include the original Google Maps forecasts, both Atlas estimates and which bus movements could be traced. [November trip records](november-model-comparison.json)

Use Python 3.12, the build requirements, 7-Zip, the official October GPS archive and the official GTFS timetable. The trainer expands one daily database at a time. Allow at least 8 GiB of scratch space and 2 GB for the evaluator. First generate a reference count from your own archive. The extractor checks each date against those locally held counts by route and receipt hour. Individual vehicle IDs and coordinates are excluded from the saved training labels.

```powershell
python -m pip install -r requirements-build.txt
$etaScratch = Join-Path $env:TEMP 'delhi-eta-comparison'
python tools/october_census.py --archive delhi-dtc-bus-gtfs-rt-2026-10.7z --scratch $etaScratch
python tools/month_eta_labels.py --scratch $etaScratch --archive delhi-dtc-bus-gtfs-rt-2026-10.7z --gtfs delhi_buses_static_gtfs_v1.zip
python tools/month_eta_labels.py --scratch $etaScratch --verify-checkpoints
python tools/evaluate_october_eta.py --labels "$etaScratch/labels" --work "$etaScratch/fit-work" --output "$etaScratch/candidate-results" --gtfs delhi_buses_static_gtfs_v1.zip
python tools/check_eta_candidate_routes.py "$etaScratch/candidate-results/november-forward" --output "$etaScratch/november-journeys.json"
```

Use a fresh output directory for a new evaluation. The output includes a summary of accuracy on the seven held-out dates and separate weekday and weekend profiles for each fit. The final command asks the router to plan four trips with each model, using identical timetables and walking routes. These are retrospective estimates. The registered field study preserves the original predictions made before the trips.
