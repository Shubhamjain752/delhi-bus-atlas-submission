from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from transit import api
from tools.build_fleet_report import (
    DATES, GROUPS, IST, LABELS, approach_age_band, case_groups, count_case, empty_counts,
    count_service_day, empty_service_days, platform_summary, timing_of_gone_offers, validate_case, verify_selection,
)


def case(day="2026-10-24", hour=0, label="gone", before=True, after=True, approaching=None):
    instant = datetime.fromisoformat(day).replace(hour=hour, tzinfo=IST)
    return {"day": day, "hour": hour, "route_id": "1996", "outcome": label, "ready": int(instant.timestamp()),
            "selected": True, "eligibility": None, "baseline": {"offered": before},
            "revised": {"offered": after, "vehicle_approaching": approaching}}


class FleetReport(unittest.TestCase):
    def test_offers_and_first_choices_are_distinct(self):
        total = empty_counts()
        for row in [case(), case(label="approaching", approaching=True),
                    case(label="gone", after=False), case(label="unverified", before=False, after=True),
                    case(label="uncertain", before=False, after=False)]:
            validate_case(row, [row["day"]])
            count_case(total, row)
        self.assertEqual(total["cases"], 5)
        self.assertEqual(total["revised"]["offered"], 3)
        self.assertEqual(total["revised_default"]["approaching"], 1)
        self.assertEqual(total["revised_secondary"]["gone"], 1)
        self.assertEqual(total["revised_secondary"]["unverified"], 1)
        self.assertEqual(total["removed"]["gone"], 1)
        self.assertEqual(total["added"]["unverified"], 1)
        self.assertEqual(total["outcomes_replayed"]["uncertain"], 1)

    def test_unselected_approach_does_not_count_as_a_planner_decision(self):
        row = case(label="approaching")
        row.update(selected=False, baseline=None, revised=None)
        total = empty_counts()
        validate_case(row, [row["day"]])
        count_case(total, row)
        self.assertEqual(total["matched"], 1)
        self.assertEqual(total["replayed"], 0)
        self.assertEqual(total["outcomes_replayed"]["approaching"], 0)
        row["selected"] = True
        with self.assertRaises(ValueError):
            validate_case(row, [row["day"]])

    def test_split_and_midnight_follow_delhi_readiness(self):
        self.assertEqual(case_groups("2026-10-23", 23), ["all", "development"])
        self.assertEqual(case_groups("2026-10-24", 0), ["all", "holdout", "overnight", "holdout_overnight"])
        self.assertNotIn("overnight", case_groups("2026-10-24", 6))
        row = case(hour=1)
        row["hour"] = 19
        with self.assertRaises(ValueError):
            validate_case(row, [row["day"]])

    def test_overnight_coverage_checks_and_receipt_latency(self):
        routes = {"1996": ("502UP", "DTC")}
        one, two = case(hour=1, approaching=True), case(hour=2)
        one.update(entry_receipt_delta_seconds=-5, exit_receipt_delta_seconds=8)
        two.update(entry_receipt_delta_seconds=-22, exit_receipt_delta_seconds=-5)
        verify_selection([one, two], routes)
        two["selected"] = False
        with self.assertRaises(ValueError):
            verify_selection([one, two], routes)
        result = timing_of_gone_offers([one, {**two, "selected": True}])["holdout_overnight"]
        self.assertEqual(result["revised"]["both_received_by_ready"], 1)
        self.assertEqual(result["revised"]["entry_or_exit_received_late"], 1)
        self.assertEqual(result["revised_default"]["both_received_by_ready"], 0)

    def test_fleet_endpoint_serves_only_published_report(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(api, "ROOT", Path(temporary)):
            data = Path(temporary) / "data"
            data.mkdir()
            (data / "fleet-boarding.pending").write_text("{}")
            with self.assertRaises(HTTPException) as error:
                api.validation("fleet_boarding")
            self.assertEqual(error.exception.status_code, 404)
            path = data / "fleet-boarding.json"
            path.write_text('{"complete":true}')
            response = api.validation("fleet_boarding")
            self.assertEqual(response.path, path)
            self.assertEqual(response.media_type, "application/json")

    def test_local_rule_parity_and_ages_use_every_matched_case(self):
        self.assertEqual([approach_age_band(k) for k in (0, 15, 16, 30, 31, 60)],
                         ["0_15", "0_15", "16_30", "16_30", "31_60", "31_60"])
        with self.assertRaises(ValueError):
            approach_age_band(61)
        day = DATES[23]
        ref = case(day=day, hour=1, approaching=True)
        ref["revised"]["gps_age_seconds"] = 30
        key = day, ref["route_id"], 1
        outcomes = ("before_exit", "after_exit", "new_exit", "lost_exit", "revised_approach",
                    "unconfirmed", "exit_overrides_approach")
        aggregate, platform = {}, {}
        for group in GROUPS:
            present = int(group in case_groups(day, 1))
            aggregate[group] = {"matched": present, "revised": {"offered": present}}
            platform[group] = {"matched": present, **{k: dict.fromkeys(LABELS, 0) for k in outcomes}}
            platform[group]["revised_approach"]["gone"] = present
        raw = [ref["route_id"], 1, ref["ready"], "gone", False, False, True, 30]
        def read(path):
            if path.name == "fleet-platform.json":
                return {"signature": "frozen", "complete": True, "matched": 1, "groups": platform,
                        "actual_offer_parity_checks": 1}
            which_day = path.name[:10]
            return {"signature": "frozen", "day": which_day, "rows": [raw] if which_day == day else []}
        seen = {key: (ref["ready"], "target", "gone", True)}
        with patch("tools.build_fleet_report.read_json", side_effect=read), patch("tools.build_fleet_report.digest", return_value="hash"):
            result = platform_summary(Path("."), aggregate, seen, [ref])
            self.assertEqual(result["holdout_approach_by_age"]["16_30"]["gone"], 1)
            ref["revised"]["gps_age_seconds"] = 29
            with self.assertRaises(ValueError):
                platform_summary(Path("."), aggregate, seen, [ref])

    def test_previous_service_day_static_boundary_is_strict(self):
        group = empty_service_days()
        row = case(hour=4)
        row["ready"] += 55 * 60 + 13
        row["service_day"] = "2026-10-23"
        count_service_day(group, row, 104113)
        count_service_day(group, {**row, "ready": row["ready"] + 1}, 104113)
        count_service_day(group, {**row, "service_day": row["day"]}, 104113)
        self.assertEqual(group["matched"], 3)
        self.assertEqual(group["same_service_day"], 1)
        self.assertEqual(group["previous"]["replayed"], 2)
        self.assertEqual(group["previous_after_static_end"]["replayed"], 1)
