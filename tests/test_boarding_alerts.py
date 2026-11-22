import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from test_router import feed
from transit.eta import EtaModel
from transit.live import LiveOverlay, protobuf
from transit.router import IST, Router


class BoardingAlerts(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        data = Path(tmp.name)
        feed(data / "service.zip")
        self.at = datetime(2026, 10, 15, 8, 13, tzinfo=IST)
        self.router = Router(data / "service.zip", data, None, None)
        self.live = LiveOverlay(EtaModel(), data).bind(self.router)
        self.router.eta = self.live

    def update(self, seconds=-3, skip=None, cancel=False, calls=("B", "C"), receipt_offset=0):
        wire = protobuf().FeedMessage()
        wire.header.gtfs_realtime_version = "2.0"
        epoch = int(self.at.timestamp())
        received = epoch + receipt_offset
        wire.header.timestamp = received
        entry = wire.entity.add(id="update").trip_update
        entry.trip.trip_id = "early"
        entry.trip.route_id = "r2"
        entry.trip.start_date = "20261015"
        entry.timestamp = received
        if cancel:
            entry.trip.schedule_relationship = entry.trip.CANCELED
        for stop in calls:
            call = entry.stop_time_update.add(stop_id=stop)
            if stop == skip:
                call.schedule_relationship = call.SKIPPED
            else:
                call.departure.time = epoch + seconds + (600 if stop == "C" else 0)
        self.live.apply_trip_updates(wire, received)

    def query(self, origin="B", destination="C", at=None):
        at = at or self.at
        with patch("transit.live.time.time", return_value=at.timestamp()):
            return self.router.journeys(origin, destination, at, asof=at)

    def test_near_miss_does_not_change_strict_journeys(self):
        self.update()
        result = self.query()
        strict = result["journeys"][0]
        self.assertEqual(strict["wait_seconds"], 12 * 60)
        self.assertEqual(strict["duration_seconds"], 22 * 60)
        self.assertEqual([l["trip_id"] for l in strict["legs"] if l["mode"] == "bus"], ["late"])
        alert, = result["boarding_alerts"]
        self.assertEqual((alert["trip_id"], alert["platform"]["id"], alert["alighting_stop"]["id"]), ("early", "B", "C"))
        self.assertEqual(alert["seconds_before_ready"], 3)
        self.assertEqual(alert["access_walk_seconds"], 0)
        self.assertFalse(alert["catchable"])
        self.assertEqual(alert["source"], "live")

    def test_conditional_option_keeps_original_forecast_and_a_reachable_fallback(self):
        from transit.contract import BoardingAlert
        self.update(seconds=-3)
        result = self.query()
        alert, = result['boarding_alerts']
        option = alert['conditional_itinerary']
        self.assertEqual(option['boarding_status'], 'only_if_bus_is_present')
        self.assertEqual(option['shift_seconds'], 3)
        self.assertEqual(alert['predicted_departure'], (self.at - timedelta(seconds=3)).isoformat())
        leg, = [l for l in option['legs'] if l['mode'] == 'bus']
        self.assertEqual((leg['trip_id'],leg['departure'],leg['arrival']),
                         ('early',self.at.isoformat(),(self.at + timedelta(minutes=10)).isoformat()))
        self.assertEqual(leg['calls'][0]['departure'], leg['departure'])
        self.assertEqual(leg['calls'][-1]['arrival'], leg['arrival'])
        self.assertGreaterEqual(len(leg['geometry']), 2)
        self.assertEqual((option['wait_seconds'],option['ride_seconds'],option['duration_seconds']), (0,600,600))
        self.assertEqual(alert['fallback_itinerary'], result['journeys'][0])
        self.assertFalse(alert['catchable'])
        BoardingAlert.model_validate(alert)

    def test_no_conditional_if_only_regular_option_boards_somewhere_else(self):
        def edit(data):
            data['stops'].append(['D','Other Platform',28.6003,77.22])
            for row in data['stop_times']:
                if row[0] == 'late' and row[1] == 'B':
                    row[1] = 'D'
        class Walk:
            def route(self,a,b):
                return {'seconds':60,'distance_m':75,'source':'osm','geometry':[a,b]}
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            feed(data/'changed.zip',edit)
            self.router = Router(data/'changed.zip',data,Walk(),None)
            self.live = LiveOverlay(EtaModel(),data).bind(self.router)
            self.router.eta = self.live
            self.update()
            result = self.query()
            self.assertTrue(result['journeys'])
            alert, = result['boarding_alerts']
            self.assertIsNone(alert.get('fallback_itinerary'))
            self.assertIsNone(alert.get('conditional_itinerary'))

    def test_explicit_per_query_eta_is_isolated_from_live_service(self):
        self.update()
        with patch('transit.live.time.time',return_value=self.at.timestamp()):
            scheduled = self.router.journeys('B','C',self.at,asof=self.at,eta=None)
            real = self.router.journeys('B','C',self.at,asof=self.at)
        self.assertEqual(scheduled['boarding_alerts'],[])
        self.assertEqual(len(real['boarding_alerts']),1)
        self.assertIs(self.router.eta,self.live)

    def test_only_direct_fresh_served_calls_can_warn(self):
        for settings in ({"skip":"B"}, {"skip":"C"}, {"cancel":True}, {"calls":("C",)}, {"seconds":0}):
            with self.subTest(settings=settings):
                self.update(**settings)
                self.assertEqual(self.query()["boarding_alerts"], [])
        self.update()
        self.assertEqual(self.query(at=self.at + timedelta(minutes=3))["boarding_alerts"], [])
        self.assertEqual(self.query(origin="A")["boarding_alerts"], [])
        self.router.eta = None
        self.assertEqual(self.query()["boarding_alerts"], [])

    def test_walk_controls_exact_ready_time_and_warning_window(self):
        class Walker:
            @staticmethod
            def route(a, b):
                return {"seconds": 120, "distance_m": 130, "geometry": [a,b], "source": "osm"}
        self.router.walking = Walker()
        self.update(seconds=60)
        alert, = self.query(origin="28.6001,77.22")["boarding_alerts"]
        self.assertEqual(alert["access_walk_seconds"], 120)
        self.assertEqual(alert["seconds_before_ready"], 60)
        self.assertEqual(alert["ready_at"], (self.at + timedelta(seconds=120)).isoformat())
        self.assertIsNotNone(alert.get('conditional_itinerary'))
        self.update(seconds=90)
        alert, = self.query(origin="28.6001,77.22",destination="28.6001,77.24")["boarding_alerts"]
        option = alert['conditional_itinerary']
        self.assertEqual([l['mode'] for l in option['legs']], ['walk','bus','walk'])
        self.assertEqual((option['access_walk_seconds'],option['egress_walk_seconds'],option['shift_seconds']), (120,120,30))
        self.assertEqual([l['seconds'] for l in option['legs']], [120,600,120])
        self.update(seconds=89)
        alert, = self.query(origin="28.6001,77.22")["boarding_alerts"]
        self.assertEqual(alert['conditional_itinerary']['shift_seconds'],31)
        self.update(seconds=59)
        self.assertFalse(self.query(origin="28.6001,77.22")["boarding_alerts"])

    def test_minute_margin_keeps_both_routes_and_the_later_wait(self):
        from transit.arrivals import platform_estimate
        from transit.contract import BoardingAlert, PossibleBoarding

        for gap in (30, 31, 60, 61):
            with self.subTest(gap=gap):
                self.update(seconds=-gap, receipt_offset=-65)
                result = self.query()
                with patch('transit.live.time.time',return_value=self.at.timestamp()):
                    platform = platform_estimate(self.router,self.live,'B','r2',self.at)
                self.assertEqual(result['journeys'][0]['wait_seconds'], 720)
                self.assertEqual(platform['wait_seconds'], 720)
                if gap > 60:
                    self.assertEqual(result['boarding_alerts'], [])
                    self.assertNotIn('possible_boarding', platform)
                    continue
                self.assertEqual(len(result['boarding_alerts']), 1)
                alert, = result['boarding_alerts']
                self.assertIn('conditional_itinerary', alert)
                self.assertIn('possible_boarding', platform)
                parsed = BoardingAlert.model_validate(alert)
                possible = PossibleBoarding.model_validate(platform['possible_boarding'])
                self.assertEqual(parsed.conditional_itinerary.shift_seconds, gap)
                self.assertEqual(parsed.seconds_before_ready, gap)
                self.assertFalse(parsed.catchable)
                self.assertEqual(alert['fallback_itinerary'], result['journeys'][0])
                self.assertEqual((possible.seconds_before_ready, possible.fallback_wait_seconds), (gap, 720))

    def test_direct_near_miss_must_improve_on_next_feasible_journey(self):
        # At 08:22, the next bus departs at 08:25 and takes the same 10 minutes.
        self.at = self.at.replace(minute=22)
        self.update()
        self.assertTrue(self.query()["journeys"])
        self.assertEqual(self.query()["boarding_alerts"], [])

    def test_expired_single_call_cannot_return_as_a_timetable_boarding(self):
        from transit.arrivals import platform_estimate

        # The nominal departure is exactly at readiness. Its fresh, sole live call has passed.
        for gap in (47, 61):
            with self.subTest(gap=gap):
                self.update(seconds=-gap, calls=('B',), receipt_offset=-65)
                result = self.query()
                bus = next(leg for leg in result['journeys'][0]['legs'] if leg['mode'] == 'bus')
                self.assertEqual(bus['trip_id'], 'late')
                with patch('transit.live.time.time',return_value=self.at.timestamp()):
                    platform = platform_estimate(self.router,self.live,'B','r2',self.at)
                self.assertEqual(platform['wait_seconds'], 720)
                if gap == 47:
                    alert, = result['boarding_alerts']
                    self.assertEqual(alert['conditional_itinerary']['shift_seconds'], gap)
                    self.assertEqual(alert['fallback_itinerary'], result['journeys'][0])
                    self.assertEqual(platform['possible_boarding']['trip_id'], 'early')
                else:
                    self.assertEqual(result['boarding_alerts'], [])
                    self.assertNotIn('possible_boarding', platform)
