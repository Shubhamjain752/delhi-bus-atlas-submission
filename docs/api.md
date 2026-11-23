# Using the journey API

Start the planner and open the [runnable guide](http://127.0.0.1:8765/api-guide). The first example accepts two places, a departure time, and returns routes, stops and a breakdown of walking, waiting and riding.

| Request | Result |
| --- | --- |
| `GET /stops?q=AIIMS` | Matching boarding platforms and their coordinates |
| `GET /journeys?from=2839&to=2363&depart=2026-11-22T15:00` | Earliest estimated destination arrivals between these platforms |
| `GET /arrivals?stop=2839&route=1882` | A platform estimate for this route at the current time |

The first journey is ranked by predicted arrival at the destination. For each bus leg, `calls[0]` gives the predicted boarding time and its source. The `same_platform_services` list shows other direct lines serving both of the selected stops. An estimate labelled `upstream_gps` in a journey uses a recent reading from this particular bus. Its `boarding_position` supplies the observed upstream stop, when the vehicle reported it, and when Atlas received the reading. Matched provider stop predictions are labelled `live`.

Without a recent usable observation, the planner can still give a timetable or historical estimate. The exact fields and request limits are in the [saved OpenAPI schema](openapi.json).
