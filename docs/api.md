# Using the journey API

A typed stop name can refer to several nearby platforms. If the first match returns no bus, Atlas checks up to two more platforms at each end with the same official name within 250 metres of that match. The response gives the selected platform IDs and flags an ambiguous name. Passing a stop ID or coordinates fixes that endpoint. The other-hour comparison uses the same resolved endpoints. In the stop picker, `via_stop` names a common next stop on services boarding there when one is available.

If the planner cannot route the last walk to a chosen pin or platform, `nearby_alighting_option` may contain a bus itinerary ending near it. Its arrival time is at the alighting stop. The distance remaining is measured in a straight line. If an independently mapped street route is available, `estimated_walk` adds its walking distance, duration and crossing warning.

Start the planner and open the [runnable guide](http://127.0.0.1:8765/api-guide). The first example accepts two places, a departure time, and returns routes, stops and a breakdown of walking, waiting and riding.

| Request | Result |
| --- | --- |
| `GET /stops?q=AIIMS` | Matching boarding platforms and their coordinates |
| `GET /journeys?from=2839&to=2363&depart=2026-11-22T15:00` | Earliest estimated destination arrivals between these platforms |
| `GET /arrivals?stop=2839&route=1882` | A platform estimate for this route at the current time |

The first journey is ranked by predicted arrival at the destination. For each bus leg, `calls[0]` gives the predicted boarding time and its source. The `same_platform_services` list shows other direct lines serving both of the selected stops. An estimate labelled `upstream_gps` in a journey uses a recent reading from this particular bus. Its `boarding_position` supplies the observed upstream stop, when the vehicle reported it, and when Atlas received the reading. Matched provider stop predictions are labelled `live`.

Without a recent usable observation, the planner can still give a timetable or historical estimate. The exact fields and request limits are in the [saved OpenAPI schema](openapi.json).

When `GET /arrivals` has two qualifying observations from the previous 30 minutes, `observed_service_gap` describes the last two distinct buses tracked through the same approach to this route's boarding platform. `gap_seconds` is the time between their observed stop passages. The response includes observation and receipt times for both buses. Some buses can be missing from the feed. Use `arrival` and `wait_seconds` for the next-bus forecast. At Shakarpur School Block on 22 November, GPS showed 37 seconds between tracked buses. Atlas estimated another 238 seconds until the next bus. [Delhi replay results](../data/observed-service-gaps.json)

### If the passenger can reach the stop sooner

`earlier_boarding_options` lists direct departures due after the requested start time but before the estimated walk to the boarding platform finishes. Each option names the boarding and exit platforms. It gives the bus’s predicted time at each, the passenger’s estimated arrival on foot (`walk_ready_at`), and the gap in seconds (`seconds_before_walk_ready`). The passenger can compare the two clocks and decide whether their own way to the stop works. The planner makes no assumption about a bike or cab. The regular journey list still uses the mapped walk.

### Evidence for a bus departure

A bus leg in `GET /journeys` can include the following fields. Route starts come from the first ordinary pickup listed for that scheduled trip in the Delhi timetable. A bus can begin passenger service at a terminal, depot stop or roadside platform.

| Field | Meaning |
| --- | --- |
| `origin_departure` | Starting stop, scheduled time and service date. Status `seen_in_service` includes the later stop where GPS saw that specific run and the observation and receipt times. Status `unconfirmed` leaves the scheduled start unverified. |
| `service_history` | Scheduled starts and qualifying recorded first-to-second-stop movements for the same route, day type and two-hour period. These two counts alone cannot establish a cancellation rate. |
| `scheduled_start_history` | A separate check of physical buses whose GPS identity matched a scheduled run. The response gives matched and scheduled counts and the dates covered. |
| `platform_gap_history` | Number of gaps, dates covered, median and 90th-percentile spacing in seconds for the boarding stop and next stop. At least five recorded gaps on three days are required. `GET /arrivals` can also return this field where the route’s next stop is unambiguous. |

The October summaries are available for November onward only on the timetable they were fitted against. Missing evidence is omitted. `GET /status` reports the loaded history under `service_history`. [Method and rebuild instructions](service-starts.md)
