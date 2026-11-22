"""Public JSON contract for the journey planner. Geometry uses latitude, longitude."""
from typing import Annotated, Literal
from datetime import date

from pydantic import AwareDatetime, BaseModel, Field


class Place(BaseModel):
    id: str | None = Field(description="Official GTFS platform ID, or null for a map pin.")
    name: str
    lat: float
    lon: float


class StopMatches(BaseModel):
    stops: list[Place]


class ReplayInfo(BaseModel):
    id: Literal["nov21"]
    received_files: int
    last_receipt: AwareDatetime | None


class BoardingEvidence(BaseModel):
    service_date: date = Field(description="GTFS operating date of this vehicle's stop forecast, in Delhi.")
    gps_age_seconds: int | None = Field(ge=0,description="Seconds since the latest matching vehicle fix available at the request. Null when no fresh, dated physical run could be corroborated.")
    vehicle_approaching: bool = Field(description="Two consecutive GPS observations moved toward the platform on its incoming published shape. The most recent observation was at most 60 seconds old at the request.")


class PossibleBoarding(BoardingEvidence):
    predicted_departure: AwareDatetime
    seconds_before_ready: int
    route: str
    trip_id: str
    fallback_wait_seconds: int
    note: str


class PlatformEstimate(BaseModel):
    platform: Place
    route_id: str
    route: str
    at: AwareDatetime = Field(description="Time the passenger is assumed to be at this platform.")
    arrival: AwareDatetime | None
    wait_seconds: int | None
    source: str | None
    observed_at: AwareDatetime | None = Field(description="Upstream GPS event used for this estimate, if any.")
    observed_stop: Place | None
    note: str
    possible_boarding: PossibleBoarding | None = None
    replay: ReplayInfo | None = None


class StopCall(BaseModel):
    stop_id: str
    arrival: AwareDatetime
    departure: AwareDatetime
    source: str = Field(description="Timing source at this call. A timetable or inferred call can follow a live anchor.")
    preceding_segment_seconds: int | None = Field(description="Previous served stop departure to this arrival. Null at boarding.")
    path_distance_m: int | None = Field(description="Distance along the aligned GTFS shape. Null when unavailable.")
    estimated_speed_kmh: float | None = Field(description="Shape distance divided by predicted segment time. Null for an unreliable or missing distance or interval.")


class Leg(BaseModel):
    start: Place = Field(alias="from")
    end: Place = Field(alias="to")
    departure: AwareDatetime
    arrival: AwareDatetime
    seconds: int
    geometry: list[tuple[float, float]] = Field(description="Ordered [latitude, longitude] pairs in WGS84.")
    source: str


class Walk(Leg):
    mode: Literal["walk"]
    distance_m: int
    origin_snap_m: float | None = None
    destination_snap_m: float | None = None
    includes_arterial_without_mapped_sidewalk: bool | None = None


class WalkingAlternative(Walk):
    assumed_pace_m_s: float
    time_saved_seconds: int | None
    reason: Literal["faster_than_bus", "no_bus_within_limits"]


class Wait(Leg):
    mode: Literal["wait"]
    transfer_buffer_seconds: int = Field(description="Minimum change allowance already included in this wait.")


class Bus(Leg):
    mode: Literal["bus"]
    route_id: str
    route: str = Field(description="Route label shown to the rider.")
    agency: str
    trip_id: str
    live_run_id: str | None = Field(default=None, description="Opaque vehicle-run identifier when multiple corroborated vehicles share this scheduled trip.")
    geometry_source: Literal["gtfs_shape", "ordered_stops"]
    model_sources: dict[str, int] = Field(description="Count of route intervals by timing source.")
    stop_count: int = Field(description="Served stops after boarding through alighting, inclusive.")
    stops: list[Place]
    skipped_stops: list[Place]
    calls: list[StopCall]


class Itinerary(BaseModel):
    departure: AwareDatetime = Field(description="Requested time to leave the origin.")
    arrival: AwareDatetime = Field(description="Estimated arrival at the destination after the final walk.")
    duration_seconds: int = Field(description="Sum of walk, wait and ride seconds.")
    transfers: int
    walk_seconds: int
    access_walk_seconds: int = Field(description="Origin to first boarding platform. Zero when already there.")
    transfer_walk_seconds: int
    egress_walk_seconds: int = Field(description="Final alighting platform to destination.")
    wait_seconds: int
    ride_seconds: int
    legs: list[Annotated[Walk | Wait | Bus, Field(discriminator="mode")]]


class SearchLimits(BaseModel):
    max_transfers: int
    horizon_seconds: int
    max_walk_m: int
    transfer_walk_m: int
    transfer_buffer_seconds: int
    endpoint_stops_considered: int
    max_transfer_candidates: int


class Comparison(BaseModel):
    departure: AwareDatetime
    fastest_seconds: int | None
    arrival: AwareDatetime | None
    warnings: list[str]


class ConditionalItinerary(Itinerary):
    boarding_status: Literal["only_if_bus_is_present"]
    shift_seconds: int = Field(gt=0, le=60)
    assumption: str


class BoardingAlert(BoardingEvidence):
    route_id: str
    route: str
    trip_id: str
    live_run_id: str | None = None
    platform: Place
    alighting_stop: Place
    predicted_departure: AwareDatetime
    ready_at: AwareDatetime = Field(description="Requested departure plus verified walking time to this platform.")
    seconds_before_ready: int = Field(gt=0, le=60)
    access_walk_seconds: int = Field(ge=0)
    source: Literal["live"]
    catchable: Literal[False]
    message: str
    conditional_itinerary: ConditionalItinerary | None = None
    fallback_itinerary: Itinerary | None = Field(default=None, description="Feasible option boarding later at the same platform, if the conditional service has left.")


class JourneyResult(BaseModel):
    origin: Place
    replay: ReplayInfo | None = None
    destination: Place
    departure: AwareDatetime
    issued_asof: AwareDatetime = Field(description="Cutoff for receiving observations used in this forecast or replay.")
    journeys: list[Itinerary] = Field(description="Ranked alternatives. Empty if no usable path was found within the search limits.")
    walking_alternative: WalkingAlternative | None = None
    boarding_alerts: list[BoardingAlert] = Field(default_factory=list, description="Direct services whose fresh live estimate just precedes the rider’s platform arrival. These are excluded from feasible itineraries.")
    warnings: list[str]
    limits: SearchLimits
    source: str
    comparison: Comparison | None = None
