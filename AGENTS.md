# Working on Delhi Bus Atlas

## Where to begin

| Area | Files | What to check |
| --- | --- | --- |
| Passenger page and map | `web/index.html`, `web/app.js`, `web/style.css` | Two endpoints, an arrival time and the walk, wait and ride breakdown |
| JSON API | `transit/api.py`, `transit/contract.py`, [OpenAPI](docs/openapi.json) | Query with coordinates, a stop name and an unknown stop |
| Service search | `transit/schedule.py`, `transit/router.py` | The service date after midnight, route direction and time available for a change |
| Walking and travel times | `transit/walking.py`, `transit/eta.py`, `transit/live.py` | Reached boarding side, GPS quality and time known at departure |
| Test evidence | `transit/field_trials.py`, `tools/`, `tests/` | Keep a later observation separate from an earlier forecast |

## Data needed

The original DTC v32 timetable is 30,058,932 bytes, with SHA-256 `8eb05d9c21e7a6b42d7b49de45cb095b70ddee5b8f7716859330c9784c322f05`. It contains 6,862 stops, 2,574 route records and 80,357 trips. Keep it zipped. A newer official feed may change platform IDs, routes and answers. Set `DELHI_GTFS_ZIP` to use a different local path. The first run writes a SQLite index in `data/`.

The Git repository supplies `data/walk.npz` and the drawing layer, `data/walk-meta.json`, the weekday and weekend aggregate ETA models and their holdout metrics. It also supplies a summary of registered forecast comparisons. Individual GPS packets, full vehicle trajectories and Google Maps screenshots are held outside this public release. The submitted prototype includes extra historical replay and evidence screens.

## Testing and rebuilding

In the virtual environment from the README, install the build dependencies. These two suites run without fetching the Delhi timetable. They use a small invented timetable to test transfers, departures after midnight and a bus that is just about to leave.

```sh
python -m pip install -r requirements-build.txt
python -m unittest discover -s tests -p test_router.py -v
python -m unittest discover -s tests -p test_boarding_alerts.py -v
```

For a city-scale check, supply v32 and run `python run.py --offline`. Open `/api-guide` and run its coordinate query. Each itinerary should satisfy walk + wait + ride = total. The original research suite requires additional raw archives and dated witness captures. Vehicle-identity exclusion IDs for that audit can be supplied locally using `DELHI_AUDIT_EXCLUDED_VEHICLES` as a comma-separated list.

To reproduce the weekday model, use the [organizer’s October GPS archive](https://delhi.transportstack.in/hackathon/) and extract the `bus_movements_YYYY_MM_DD.db` files for 12, 13 and 15 October into a local folder. From the repository root run:

```sh
python tools/train_eta.py --db-dir PATH_TO_DAILY_DATABASES
```

Training uses 12–13 October. The held-out comparison uses 15 October. For the weekend fit, use the same archive, retain 10, 11 and 17 October and run:

```sh
python tools/train_eta.py --db-dir PATH_TO_DAILY_DATABASES --data-dir data/weekend --train 2026-10-10 2026-10-11 --holdout 2026-10-17 --late-evening
```

The [walking build](docs/walking.md) explains foot access, map coverage and its source checksum. For live operation, omit `--offline`. The public Reclaim feed can supply updates; the optional official feed needs an authorized key in `DELHI_GTFS_RT_KEY`. Keep keys outside Git.
