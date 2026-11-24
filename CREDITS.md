# Credits

Team IITBOYS built Delhi Bus Atlas for Problem 1 of the Delhi Transport Stack Hackathon. Team members are Shubham Jain, Akhil Goyal and Keshav Gilra. OpenAI Codex assisted with programming, testing, documentation and submission preparation. The travel-time estimates use bus GPS, a statistical model and a timetable.

Delhi Transport Stack, DTC and the Delhi Department of Transport supplied the bus schedule and the October 2026 bus-position archive. Reclaim’s [India GTFS](https://github.com/reclaimchennai/India_GTFS) provides the optional community live feed. Google Maps supplied comparison forecasts. The timetable is obtained directly from its [publisher](https://delhi.transportstack.in/data-services).

Map data © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright), distributed under the [Open Database License 1.0](https://opendatacommons.org/licenses/odbl/1-0/). The bundled derived walking network and map layer use the same licence. The source extract comes from [Geofabrik](https://download.geofabrik.de/asia/india/northern-zone.html). Source, checksum and geographic bounds are in [walk-meta.json](data/walk-meta.json).

The browser uses [Leaflet](web/vendor/leaflet/LICENSE) and [Swagger UI](web/vendor/swagger/LICENSE), with notices in their vendor folders. The Python server uses FastAPI, Pydantic, Uvicorn, NumPy, SciPy, Requests, GTFS Realtime Bindings and Protocol Buffers. Build and evaluation tools also use pandas, pyosmium, Shapely and Numba. Playwright and Chromium supported browser verification. Each dependency retains its own licence.

The [pedestrian crossing file](data/walk-crossings.json) is also derived from OpenStreetMap and distributed under ODbL 1.0. Its Delhi extract comes from OpenStreetMap France. The file records the source URL, checksum, mapped crossings and estimated street connections used by the planner. [Crossing builder](tools/build_crossing_access.py)

The [FOSSGIS walking router](https://routing.openstreetmap.de/) supplied the separate street estimate for the two Nehru Place platforms. Its [route geometry and source requests](data/walk-estimates.json) use OpenStreetMap data under ODbL 1.0.

The [MIT License](LICENSE) covers the original Atlas code and documentation by Shubham Jain, Akhil Goyal and Keshav Gilra of Team IITBOYS. Materials in `data/`, external comparison material and third-party software remain subject to their respective publishers’ terms and notices.
