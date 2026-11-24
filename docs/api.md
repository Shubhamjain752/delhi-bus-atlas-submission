# Using the journey API

A typed stop name can refer to several nearby platforms. If the first match returns no bus, Atlas checks up to two more platforms at each end with the same official name within 250 metres of that match. The response gives the selected platform IDs and flags an ambiguous name. Passing a stop ID or coordinates fixes that endpoint. The other-hour comparison uses the same resolved endpoints. In the stop picker, `via_stop` names a common next stop on services boarding there when one is available.

If the planner cannot route the last walk to a chosen pin or platform, `nearby_alighting_option` may contain a bus itinerary ending near it. Its arrival time is at the alighting stop. The distance remaining is measured in a straight line. If an independently mapped street route is available, `estimated_walk` adds its walking distance, duration and crossing warning.

Start the planner and open the [runnable guide](http://127.0.0.1:8765/api-guide). The first example accepts two places, a departure time, and returns routes, stops and a breakdown of walking, waiting and riding.

| Request | Result |
| --- | --- |
| `GET /stops?q=AIIMS` | Matching boarding platforms and their coordinates |
| `GET /journeys?from=2839&to=2363&depart=2026-11-22T15:00` | Earliest estimated destination arrivals between these platforms |
| `GET /journeys?from=2433&to=9561&depart=2026-11-24T10:57&preference=balanced&limit=5` | Compare an earlier arrival with a shorter walk from Vivek Vihar to Rajiv Chowk |
| `GET /arrivals?stop=2839&route=1882` | A platform estimate for this route at the current time |

The API normally lists the earliest estimated arrival first. The passenger map requests `preference=balanced`. Atlas looks at routes arriving within 15 minutes of the earliest one to find a realistic shorter walk. Its shorter-walk group includes routes with less than five extra minutes on foot compared with the shortest walk in that window. Within that group, earlier arrivals take priority. Atlas also keeps a place for a direct bus when available.

Atlas can offer up to two routes that ask for a longer walk. Each must save at least two minutes or 10% of the full trip compared with the earliest shorter-walk choice. Only a saving of at least 10% can put a longer walk first. A second longer-walk option must reduce walking by at least five minutes or require fewer changes than the first. These comparisons use seconds before rounding. The separate `compare_hour` result gives the fastest journey at that hour.

If the first 15-minute window yields fewer than three shorter walks, Atlas looks for later services with similar or less walking. It allows up to half the fastest journey’s duration in extra travel time, with a minimum allowance of 30 minutes and a maximum of 45. To fill a remaining place among the first three, it can include a different service with at most 15 minutes of total walking inside that time allowance. Its card shows the later arrival and total walking time.

The map displays the first three choices together and up to two under **More options**. `limit` can request fewer results. `balanced_choices.primary_count` gives the number to show immediately. When supplied, `faster_index` and `less_walking_index` identify the options used to explain a trade-off. `time_saved_seconds` and `extra_walk_seconds` give the size of that difference. Departures a passenger might miss appear in the separate boarding advice.

For each bus leg, `calls[0]` gives the predicted boarding time and its source. The `same_platform_services` list shows other direct lines serving both of the selected stops. An estimate labelled `upstream_gps` in a journey uses a recent reading from this particular bus. Its `boarding_position` supplies the observed upstream stop, when the vehicle reported it, and when Atlas received the reading. Matched provider stop predictions are labelled `live`.

Each bus leg also includes `origin_departure` for its dated run. `scheduled_at` is the scheduled time at the first public stop. The leg’s `departure` is the estimated pickup at the passenger’s stop. `as_of` records the latest time at which the planner could have received evidence. The status applies to that run alone.

| Status | What the GPS evidence supports |
| --- | --- |
| `unconfirmed` | There is no qualifying observation for that departure. The bus remains eligible. |
| `at_origin` | A matched vehicle was seen at the first public stop. Whether it has left is unknown. |
| `seen_in_service` | GPS placed a matched vehicle farther along the route. The time it left its first stop is unknown. |
| `confirmed_departure` | GPS followed the same scheduled run from its first public stop to the next. The response gives the observation and receipt times for both stops. |

A confirmed start records a past event. It remains available for that run if its GPS signal fades. The hosted planner holds observations on each active server instance. After a restart, or for a past date without a loaded recording, a bus may show as unconfirmed. An estimate based on past journeys can accompany any status. The map’s **Recommended** and **Earliest arrival** settings request `preference=balanced` and `preference=earliest` respectively. [See the 808STDOWN example](november-departure-audit.md) · [Saved OpenAPI schema](openapi.json)

When `GET /arrivals` has two qualifying observations from the previous 30 minutes, `observed_service_gap` describes the last two distinct buses tracked through the same approach to this route's boarding platform. `gap_seconds` is the time between their observed stop passages. The response includes observation and receipt times for both buses. Some buses can be missing from the feed. Use `arrival` and `wait_seconds` for the next-bus forecast. At Shakarpur School Block on 22 November, GPS showed 37 seconds between tracked buses. Atlas estimated another 238 seconds until the next bus. [Delhi replay results](../data/observed-service-gaps.json)

### If the passenger can reach the stop sooner

`earlier_boarding_options` lists direct departures due after the requested start time but before the estimated walk to the boarding platform finishes. Each option names the boarding and exit platforms. It gives the bus’s predicted time at each, the passenger’s estimated arrival on foot (`walk_ready_at`), and the gap in seconds (`seconds_before_walk_ready`). The passenger can compare the two clocks and decide whether their own way to the stop works. The planner makes no assumption about a bike or cab. The regular journey list still uses the mapped walk.
