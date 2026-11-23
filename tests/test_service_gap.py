from datetime import datetime
from pathlib import Path
import tempfile
import unittest

from transit.arrivals import platform_estimate
from transit.contract import PlatformEstimate
from transit.eta import EtaModel, IST
from transit.live import protobuf
from transit.replay import CapturedOverlay
from transit.router import Router
from test_disruptions import add_services
from test_router import feed


def lines(content):
    add_services(content)
    content['trips'][0].append('direction_id')
    for trip in content['trips'][1:]:
        trip.append('0')
    content['trips'].append(['line', 's', 'branch', '', '1'])
    for i, stop in enumerate('RYZ'):
        clock = f'09:0{i}:00'
        content['stop_times'].append(['branch', stop, i, clock, clock])


class ObservedServiceGap(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        folder = Path(temp.name)
        feed(folder / "stops.zip", lines)
        self.when = datetime(2026, 10, 15, 0, 5, tzinfo=IST)
        self.issue = int(self.when.timestamp())
        self.router = Router(folder / "stops.zip", folder, None, None)
        self.overlay = CapturedOverlay(EtaModel(), folder, self.issue).bind(self.router)

    def snapshot(self, offset, *positions, delay=4, provider="community_positions", direction=None, service_date=None):
        observed = self.issue + offset
        packet = protobuf().FeedMessage()
        packet.header.gtfs_realtime_version = "2.0"
        packet.header.timestamp = observed
        for index, (bus, stop) in enumerate(positions):
            v = packet.entity.add(id=str(index)).vehicle
            v.vehicle.id, v.trip.route_id, v.trip.trip_id = bus, "line", "trip-" + bus
            if direction is not None:
                v.trip.direction_id = direction
            if service_date:
                v.trip.start_date = service_date
            v.timestamp = observed
            v.position.latitude = self.router.stops[stop]["lat"]
            v.position.longitude = self.router.stops[stop]["lon"]
        self.overlay.apply_positions(packet, observed + delay, provider)

    def response(self):
        return platform_estimate(self.router, self.overlay, "Y", "line", self.when)

    def test_two_buses_share_a_directed_approach_across_midnight(self):
        self.snapshot(-435, ("one", "X"))
        self.snapshot(-315, ("one", "Y"))
        self.snapshot(-180, ("one", "Y"))
        self.snapshot(-130, ("two", "X"))
        self.snapshot(-10, ("two", "Y"))
        answer = self.response()
        gap = PlatformEstimate.model_validate(answer).observed_service_gap
        self.assertEqual(gap.approach_stop_id, "X")
        self.assertEqual((gap.gap_seconds, gap.window_seconds), (305, 1800))
        self.assertEqual(gap.previous_observed_at.isoformat(), "2026-10-14T23:59:45+05:30")
        self.assertEqual(int(gap.latest_received_at.timestamp()), self.issue-6)
        self.assertEqual(int(gap.previous_received_at.timestamp()), self.issue-311)
        self.assertNotIn("vehicle", str(answer["observed_service_gap"]))
        self.assertIsNone(answer['wait_seconds'])

    def test_single_vehicle_wrong_approach_or_ambiguous_identity_abstains(self):
        self.snapshot(-295, ("one", "X"))
        self.snapshot(-175, ("one", "Y"))
        self.snapshot(-150, ("one", "Y"), ("two", "Z"))
        self.snapshot(-28, ("one", "Y"), ("two", "Y"))
        self.snapshot(-15, ("three", "Y"), ("three", "X"))
        self.assertIsNone(self.response().get("observed_service_gap"))

    def test_future_receipt_abstains(self):
        self.snapshot(-310, ("one", "X"))
        self.snapshot(-185, ("one", "Y"))
        self.snapshot(-130, ("two", "X"))
        self.snapshot(-5, ("two", "Y"), delay=7)
        self.assertIsNone(self.response().get("observed_service_gap"))

    def test_unrelated_service_date_abstains(self):
        self.snapshot(-310, ('one', 'X'), service_date='20261014')
        self.snapshot(-185, ('one', 'Y'), service_date='20261014')
        self.snapshot(-130, ('two', 'X'), service_date='20251014')
        self.snapshot(-10, ('two', 'Y'), service_date='20251014')
        self.assertIsNone(self.response().get('observed_service_gap'))

    def test_tomorrows_service_date_cannot_have_passed(self):
        self.snapshot(-310, ('one', 'X'), service_date='20261014')
        self.snapshot(-185, ('one', 'Y'), service_date='20261014')
        self.snapshot(-130, ('two', 'X'), service_date='20261016')
        self.snapshot(-10, ('two', 'Y'), service_date='20261016')
        self.assertIsNone(self.response().get('observed_service_gap'))

    def test_collection_break_resets_window(self):
        self.snapshot(-600, ('one', 'X'))
        self.snapshot(-480, ('one', 'Y'))
        self.snapshot(-130, ('two', 'X'))
        self.snapshot(-10, ('two', 'Y'))
        self.assertIsNone(self.response().get('observed_service_gap'))

    def test_provider_change_resets_window(self):
        self.snapshot(-310, ('one', 'X'))
        self.snapshot(-190, ('one', 'Y'))
        self.snapshot(-130, ('two', 'X'), provider='official_positions')
        self.snapshot(-10, ('two', 'Y'), provider='official_positions')
        self.assertIsNone(self.response().get('observed_service_gap'))

    def test_different_branches_and_conflicting_feed_direction_abstain(self):
        self.snapshot(-430, ('one', 'X'))
        self.snapshot(-310, ('one', 'Y'))
        self.snapshot(-300, ('two', 'R'))
        self.snapshot(-165, ('three', 'X'), direction=1)
        self.snapshot(-80, ('two', 'Y'))
        self.snapshot(-45, ('three', 'Y'), direction=1)
        self.assertIsNone(self.response().get('observed_service_gap'))
        self.assertEqual([p.approach for p in self.overlay.service_gap_snapshot[-1]['line', 'Y']], ['X', 'R'])

    def test_old_passages_expire_while_other_packets_continue_arriving(self):
        self.snapshot(-2140, ('one', 'X'))
        self.snapshot(-2020, ('one', 'Y'))
        self.snapshot(-1980, ('two', 'X'))
        self.snapshot(-1860, ('two', 'Y'))
        for offset in range(-1740, 0, 120):
            self.snapshot(offset)
        self.assertIsNone(self.response().get('observed_service_gap'))
        self.assertFalse(self.overlay.service_gap_snapshot[-1])

    def test_fresh_last_passage_with_stale_feed_abstains(self):
        self.snapshot(-650, ('one', 'X'))
        self.snapshot(-530, ('one', 'Y'))
        self.snapshot(-420, ('two', 'X'))
        self.snapshot(-300, ('two', 'Y'))
        self.assertIsNone(self.response().get('observed_service_gap'))
