# Testing a month of travel times

The deployed version of Delhi Bus Atlas learns weekday and weekend travel times from four October dates. This experiment asks whether more history improves predictions on later days and reaches routes or hours that previously had little data.

## Comparison fixed before scoring

- Use 1–24 October for the candidate fit. Keep observations from 25–31 October out of that fit. The seven test dates include five weekdays and two weekend dates.
- Run the published weekday and weekend profiles and the candidate on the same observed, directed movements between consecutive stops. Include all usable route IDs and hours in the archive, including after midnight. Report which route IDs, hours or dates supplied no usable observations.
- Give each profile the same timetable duration. Compare the historical estimates before adding any live correction. The result measures the benefit of additional history. First-bus waiting, transfers and a passenger’s door-to-door time require separate tests.
- For each usable movement, score the predicted seconds against elapsed time from GPS. Report the average absolute error, the error within which 90% of observations fall, signed bias and observation count. Show weekdays, weekends and overnight separately, as well as the routes or stop pairs on which only the new fit can use history.
- Keep the published 15 and 17 October results with the four-day model. Those dates are part of the candidate’s 1–24 October fit. Only the later test dates provide fresh evidence for this comparison.

After evaluation, a separate fit may use all 31 dates for forecasts in November. The October test result belongs to the 1–24 October fit. Testing the 31-day version requires subsequent trips. All candidate files are kept separate from the two profiles used on Vercel. The 5:30 p.m. Rohini forecasts remain the versions recorded before departure.

## Reproduce the experiment

Use Python 3.12, the build requirements, 7-Zip, the official October GPS archive and the official GTFS timetable. The trainer reads the original archive and expands one daily database at a time. Choose a scratch directory on a drive with space for that day and the derived files. The biggest source day takes 4.51 GiB when expanded. First generate a reference count from your own archive. The extractor checks each date against those locally held counts by route and receipt hour. Individual vehicle IDs and coordinates are excluded from the saved training labels.

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
