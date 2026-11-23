"""Measure extra waiting for the recommended line in a saved first-direct-bus replay."""
import argparse
from collections import defaultdict
import json
from pathlib import Path
from statistics import median
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.check_first_direct_bus import read, winner


def wait_costs(decisions, witnesses):
    """Pair the two choices only when the same GPS record can score both."""
    first, _ = winner(witnesses, 0)
    if first is None or any(decisions[side] is None for side in ("before", "after")):
        return None
    seen = witnesses["complete"]
    lines = [decisions[side]["line"] for side in ("before", "after")]
    if any(line not in seen for line in lines):
        return None
    return tuple(seen[line][0] - seen[first][0] for line in lines)


def summary(waits, total):
    result = {"queries": total, "paired_observed_choices": len(waits)}
    for index, side in enumerate(("before", "after")):
        seconds = [pair[index] for pair in waits]
        result[side] = {
            "median_extra_wait_seconds": median(seconds) if seconds else None,
            "within_five_minutes": sum(value <= 300 for value in seconds),
        }
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scratch", type=Path, required=True, help="Prepared and scored first-direct-bus replay directory")
    parser.add_argument("--run-id", required=True, help="Signature from the scored-native-<signature>.gz filenames")
    parser.add_argument("--study", type=Path, default=ROOT / "data/first-direct-bus.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    study = json.loads(args.study.read_bytes())
    waits = defaultdict(list)
    totals = defaultdict(int)
    for day in study["dates"]:
        day_dir = args.scratch / day
        forecasts = read(day_dir / f"scored-native-{args.run_id}.gz")
        pending = {(r["hour"], r["board"], r["exit"]): r for r in forecasts}
        if len(pending) != len(forecasts):
            raise ValueError(f"Duplicate forecast on {day}")
        for hour in range(24):
            observations = read(day_dir / f"{hour:02}.gz")
            if observations.get("truth_version") != 2:
                raise ValueError(f"Expected version 2 same-bus stop observations on {day}")
            groups = ("all", "overnight") if hour < 5 else ("all",)
            for (board, exit_stop, _), observed in zip(observations["cases"], observations["witnesses"], strict=True):
                decision = pending.pop((hour, board, exit_stop))
                costs = wait_costs(decision, observed)
                for group in groups:
                    totals[group] += 1
                    if costs is not None:
                        waits[group].append(costs)
        if pending:
            raise ValueError(f"Missing outcome records on {day}")
    result = {"dates": study["dates"], "run_id": args.run_id,
              "measure": "Seconds from the first verified line at the boarding platform to the first verified passage of the recommended line. Rider already at stop.",
              "all": summary(waits["all"], totals["all"]),
              "overnight": summary(waits["overnight"], totals["overnight"])}
    counts = study["by_period"]["all"]["board"]["counts"]
    if (totals["all"], len(waits["all"])) != (counts["queries"], counts["both_picks_fully_witnessed"]):
        raise ValueError("The supplied forecasts and observations do not match this study's paired cohort")
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
