"""HTTP entry points for the Delhi bus journey planner."""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
import json
import logging
import os
import threading
import time
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .arrivals import platform_estimate
from .contract import JourneyResult, StopMatches
from .eta import EtaModel, IST
from .field_trials import field_results
from .router import Router
from .endpoints import nearby_alighting_option, plan_with_stop_names, suggestions
from .replay import load_november_capture
from .route_observation import ObservationOverlay,PlatformResponse,recorded_restrictions,route_observation
from .walking import Walking
from .walking_option import walking_alternative


ROOT = Path(__file__).resolve().parents[1]
LOG = logging.getLogger("uvicorn.error")


def ist_departure(value):
    if value is None:
        return datetime.now(IST).replace(microsecond=0)
    if len(value) < 16 or value[10] not in ("T", " "):
        raise ValueError("Use an ISO date and time, for example 2026-10-15T08:45:00+05:30.")
    dt = datetime.fromisoformat(value)
    return dt.astimezone(IST) if dt.tzinfo else dt.replace(tzinfo=IST)


class Planner:
    def __init__(self, root, online):
        self.directory = root / "data"
        schedule = Path(os.getenv("DELHI_GTFS_ZIP") or root / "delhi_buses_static_gtfs_v1.zip")
        self.walking = Walking(self.directory, use_crossing_access=True)
        self.model = EtaModel.load(self.directory, include_forward=True)
        self.live = ObservationOverlay(self.model, self.directory)
        self.router = Router(schedule, self.directory, self.walking, self.live)
        self.live.bind(self.router)
        self.archived_feed = lru_cache(maxsize=2)(
            lambda issue: load_november_capture(self.router, self.model, self.directory, issue))
        self.online = online
        self.stop_polling = threading.Event()
        self.interest = threading.Event()
        self.last_interest = time.monotonic()
        self.thread = None
        if online:
            self.thread = threading.Thread(target=self.poll, daemon=True, name="delhi-bus-feed")
            self.thread.start()

    def poll(self):
        while not self.stop_polling.is_set():
            if time.monotonic() - self.last_interest > 150:
                self.interest.wait(60)
                self.interest.clear()
                continue
            try:
                self.live.fetch()
            except Exception as error:
                # A provider failure must leave timetable/replay routing available.
                LOG.warning("Live update unavailable (%s).", type(error).__name__)
            self.stop_polling.wait(45)

    def close(self):
        self.stop_polling.set()
        self.interest.set()
        if self.thread:
            self.thread.join(timeout=3)
        self.archived_feed.cache_clear()

    def info(self):
        self.touch()
        stats = self.router.stats
        version = next((f.get("feed_version") for f in stats["feed_info"]), None)
        live = self.live.status() if self.online else {"state":"disabled", "source":"offline"}
        return {"feed_version":version, "timetable":stats, "walking":self.walking.metadata,
                "model":self.model.metadata, "live":live, "timezone":"Asia/Kolkata"}

    def touch(self):
        self.last_interest = time.monotonic()
        self.interest.set()

    def query(self, origin, destination, dt, limit, transfers, replay=None, *, include_nearby=True, preference="earliest"):
        now = datetime.now(IST)
        if not replay and abs((dt - now).total_seconds()) < 3600:
            self.touch()
        # A near-future journey can use observations received by the actual query time.
        issue = now if not replay and timedelta(0) <= dt-now <= timedelta(minutes=45) else dt
        overlay, recording = self.archived_feed(int(issue.timestamp())) if replay else (self.live, None)
        options = dict(limit=limit, max_transfers=transfers, asof=issue, eta=overlay, preference=preference)
        result = plan_with_stop_names(self.router, origin, destination, dt, **options)
        if include_nearby and not result["journeys"]:
            nearby = nearby_alighting_option(self.router, result, dt, **options)
            if nearby:
                result["nearby_alighting_option"] = nearby
        if recording:
            result["replay"] = recording
        result["walking_alternative"] = walking_alternative(self.walking, result)
        result["issued_asof"] = issue.isoformat(timespec="seconds")
        return result


@asynccontextmanager
async def lifespan(application):
    instance = Planner(ROOT, os.getenv("DELHI_LIVE", "1").lower() not in {"0","off","false"})
    application.state.planner = instance
    try:
        yield
    finally:
        instance.close()


app = FastAPI(title="Delhi Bus Atlas", version="0.9.0", lifespan=lifespan, docs_url=None,
              description="Plan fixed-route bus journeys in Delhi. Times include a UTC offset. Durations are seconds. "
                          "See the [API guide](/api-guide) for a runnable coordinate query and response interpretation.")


@app.get("/docs", include_in_schema=False)
def interactive_docs():
    return get_swagger_ui_html(openapi_url="/openapi.json", title="Delhi Bus Atlas · API reference",
                               swagger_js_url="/vendor/swagger/swagger-ui-bundle.js",
                               swagger_css_url="/vendor/swagger/swagger-ui.css",
                               swagger_favicon_url="/vendor/swagger/favicon-32x32.png",
                               swagger_ui_parameters={"validatorUrl":None, "defaultModelsExpandDepth":0})


@app.get("/status")
def status():
    """Inspect timetable version, observational coverage and live-feed freshness."""
    return app.state.planner.info()


@app.get("/stops", response_model=StopMatches, response_model_exclude_none=True, tags=["Journey planning"])
def stops(q: str = Query(min_length=1, max_length=150), limit: int = Query(10, ge=1, le=30)):
    """Find boarding platforms by name or identifier."""
    return {"stops":suggestions(app.state.planner.router, q.strip(), limit)}


@app.get("/journeys", response_model=JourneyResult, response_model_exclude_unset=True, tags=["Journey planning"])
def journeys(origin: str = Query(alias="from", min_length=1, max_length=160,
                                 description="Origin as latitude,longitude, stop ID, or stop name.", examples=["28.71815,77.06380"]),
             destination: str = Query(alias="to", min_length=1, max_length=160,
                                      description="Destination in the same formats.", examples=["28.70700,77.17890"]),
             depart: str | None = Query(None, max_length=40, description="ISO 8601 departure. A timezone-free value uses Delhi time. Defaults to now.",
                                        examples=["2026-10-15T08:45"]),
             limit: int = Query(3, ge=1, le=6),
             preference: Literal["earliest","balanced"] = Query("earliest", description="Choose earliest arrival or weigh extra walking against the time saved. The public map uses balanced."),
             max_transfers: int = Query(1, ge=0, le=1),
             compare_hour: int | None = Query(None, ge=0, le=23),
             replay: Literal["nov21"] | None = Query(None, description="Use feeds received by the requested November capture clock.")):
    """Plan from a stop ID, stop name or latitude,longitude. Optionally compare another hour on the same date."""
    planner = app.state.planner
    try:
        dt = ist_departure(depart)
        if replay and compare_hour is not None:
            raise ValueError("The November capture covers part of one hour. Omit compare_hour for this replay.")
        result = planner.query(origin.strip(), destination.strip(), dt, limit, max_transfers, replay, preference=preference)
        if compare_hour is not None:
            other = dt.replace(hour=compare_hour, minute=0, second=0, microsecond=0)
            compare = planner.query(result["origin"], result["destination"], other, 1, max_transfers, include_nearby=False)
            fastest = next(iter(compare["journeys"]), {})
            result["comparison"] = {"departure":other.isoformat(), "fastest_seconds":fastest.get("duration_seconds"),
                                    "arrival":fastest.get("arrival"), "warnings":compare.get("warnings", [])}
    except (ValueError, OverflowError) as error:
        raise HTTPException(422, str(error)) from error
    except FileNotFoundError as error:
        raise HTTPException(503, "Local journey data is unavailable.") from error
    return result


@app.get("/arrivals", response_model=PlatformResponse, tags=["Journey planning"])
def arrivals(stop: str = Query(min_length=1, max_length=100, description="GTFS boarding platform ID.", examples=["1525"]),
             route: str = Query(min_length=1, max_length=100, description="route_id from the journey bus leg.",examples=["1407"]),
             at: str | None = Query(None, max_length=40, description="Platform arrival time. Defaults to now."),
             replay: Literal["nov21"] | None = Query(None, description="Replay the recorded November feed at the specified time.")):
    """Estimate the next service on a route for someone already at the selected platform."""
    planner = app.state.planner
    try:
        when = ist_departure(at)
        if not replay and abs((when-datetime.now(IST)).total_seconds()) < 3600:
            planner.touch()
        overlay, recording = planner.archived_feed(int(when.timestamp())) if replay else (planner.live, None)
        if recording:
            recorded_restrictions(overlay,planner.directory,int(when.timestamp()))
        result = platform_estimate(planner.router, overlay, stop, route, when)
        movement = route_observation(planner.router,overlay,stop,route,when,result)
        if movement:
            result['route_observation'] = movement
        if recording:
            result["replay"] = recording
        return result
    except (ValueError, OverflowError) as error:
        raise HTTPException(422, str(error)) from error
    except FileNotFoundError as error:
        raise HTTPException(503, "Local platform data is unavailable.") from error


@app.get("/map/context", include_in_schema=False)
def map_context():
    path = ROOT / "data" / "map-context.geojson.gz"
    if not path.exists():
        return {"type":"FeatureCollection", "features":[]}
    return FileResponse(path, media_type="application/geo+json", headers={
        "Content-Encoding":"gzip", "Cache-Control":"public, max-age=86400"})


@app.get("/validation", tags=["Evaluation"])
def validation(day_type: Literal["weekday", "weekend", "journeys", "matched", "waits", "wait_confirmation", "speed", "vehicles",
                                "near_miss", "near_miss_router", "near_miss_confirmation", "month_boarding",
                                "month_tickets", "fleet_boarding", "fleet_platform", "fleet_short_links", "fleet_provider_clocks",
                                "anonymous_route_observation", "anonymous_route_observation_confirmation", "provider_clock_arrivals"] = "weekday"):
    """Read a measured holdout or the matched-coordinate Google Maps and GPS comparisons."""
    files = {"weekday":"eta_metrics.json", "weekend":"weekend/eta_metrics.json",
             "journeys":"journey-validation.json", "matched":"nov21-planner-replay.json",
             "waits":"wait-validation.json", "wait_confirmation":"wait-confirmation.json",
             "speed":"gps-speed.json", "vehicles":"vehicle-identity-validation.json",
             "near_miss":"near-miss-peak.json", "near_miss_router":"near-miss-router.json",
             "near_miss_confirmation":"near-miss-confirmation.json", "month_boarding":"month-boarding.json",
             "month_tickets":"month-ticket-check.json", "fleet_boarding":"fleet-boarding.json",
             "fleet_platform":"fleet-platform.json", "fleet_short_links":"fleet-short-links.json",
             "fleet_provider_clocks":"fleet-provider-clocks.json",
             "anonymous_route_observation":"fleet-anonymous-arrivals/receipt-report.json",
             "anonymous_route_observation_confirmation":"fleet-anonymous-arrivals/receipt-confirmation.json",
             "provider_clock_arrivals":"fleet-arrivals-fallback.json"}
    path = ROOT / "data" / files[day_type]
    if not path.exists():
        raise HTTPException(404, "Run the historical evaluation to generate metrics.")
    if day_type.startswith(("fleet_", "anonymous_route_observation")) or day_type == "provider_clock_arrivals":
        return FileResponse(path, media_type="application/json")
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/api-guide", include_in_schema=False)
def api_guide():
    return FileResponse(ROOT / "web" / "api-guide.html", media_type="text/html")


@app.get("/submission/{filename}", include_in_schema=False)
def submission_pdf(filename: str):
    """Serve the three judge documents included with the submission."""
    if filename not in {"01-proposal.pdf", "02-technical-note.pdf", "05-validation.pdf"}:
        raise HTTPException(404, "Unknown submission document.")
    path = ROOT / "submission" / filename
    if path.is_file():
        return FileResponse(path, media_type="application/pdf")
    # A standalone offline app can be extracted separately from its submission PDFs.
    return RedirectResponse("https://raw.githubusercontent.com/Shubhamjain752/delhi-bus-atlas-submission/main/submission/" + filename)


@app.get("/field-checks", include_in_schema=False)
def field_checks():
    return FileResponse(ROOT / "web" / "field-trials.html", media_type="text/html")


@app.get("/field-trials/results", tags=["Evaluation"])
def prospective_results():
    """Return the frozen trip forecasts and any dated, GPS-supported passenger simulations."""
    try:
        return field_results(ROOT / "data")
    except FileNotFoundError:
        return FileResponse(ROOT / "data" / "public-field-results.json", media_type="application/json")


app.mount("/", StaticFiles(directory=ROOT/"web", html=True), name="rider")
