"""Run the registered GPS scorer with consistent whole-second walking clocks.

The original scoring file and its registration hash are kept for independent replay.
Only midpoint display and duration accounting change. Vehicle choice uses the
original observation bounds and the same boarding, branch and transfer checks.
"""
import importlib.util
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import score_simulated_rides as registered


def ride_case(scenario, candidates, distances, pace, transfer_buffer, aligned, flags=None):
    # Ask the registered implementation to follow all vehicle legs. A withheld
    # egress distance stops it immediately before its original final accounting.
    result = registered.ride_case(scenario, candidates, [*distances[:-1], None], pace, transfer_buffer, aligned, flags)
    events = result["events"]
    for previous, current in zip(events, events[1:]):
        if current["kind"] == "ready_at_stop":
            current["at"] = registered.stamp(registered.epoch(previous["at"]) + current["walking_seconds"])
    if result["status"] != "no_independently_mapped_final_walk" or distances[-1] is None:
        return result

    alight = events[-1]
    seconds = round(distances[-1] / pace)
    arrival = {key: registered.stamp(registered.epoch(alight[key]) + seconds) for key in ("at", "lower", "upper")}
    events.append({"kind":"arrive_destination", "source":"walk_model", **arrival,
                   "walk_metres":distances[-1], "walking_seconds":seconds})
    components = {"ready_at_stop":0, "arrive_destination":0, "board":0, "alight":0}
    for previous, current in zip(events, events[1:]):
        duration = registered.epoch(current["at"]) - registered.epoch(previous["at"])
        if duration < 0 or "walking_seconds" in current and duration != current["walking_seconds"]:
            raise ValueError("Inconsistent simulated stage clocks")
        components[current["kind"]] += duration
    result.update(status="complete_simulation", walking_seconds=components["ready_at_stop"] + components["arrive_destination"],
                  waiting_seconds=components["board"], riding_seconds=components["alight"],
                  door_to_door_seconds_proxy=sum(components.values()), arrival_interval_ist=[arrival["lower"],arrival["upper"]])
    return result


# Load the frozen CLI into a separate module so original callers and registered
# controls can still use their exact version in the same Python process.
spec = importlib.util.spec_from_file_location("_registered_dated_gps", registered.__file__)
engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine)
engine.ride_case = ride_case
registered_run = engine.run


def run(scenario, source, horizon_minutes, archive_member=None):
    output = registered_run(scenario, source, horizon_minutes, archive_member)
    output["clock_accounting"] = {"revision":"1.1", "registered_scorer_sha256":registered.fingerprint(Path(registered.__file__)),
                                  "corrected_scorer_sha256":registered.fingerprint(Path(__file__)),
                                  "change":"Walking durations advance the preceding displayed midpoint. GPS interval bounds and bus choices use the registered rules."}
    return output


engine.run = run

if __name__ == "__main__":
    engine.main()
