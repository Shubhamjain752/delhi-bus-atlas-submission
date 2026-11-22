from datetime import datetime, timedelta
import gzip
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_router import feed
from transit.arrivals import platform_estimate
from transit.contract import PlatformEstimate
from transit.eta import EtaModel
from transit.live import LiveOverlay, protobuf
from transit.replay import load_november_capture
from transit.router import IST, Router


class PlatformReplay(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.capture = self.root / 'nov21-samples'
        self.capture.mkdir()
        def extend_calendar(tables):
            tables['calendar'][1][-1] = '20261130'
        feed(self.root/'schedule.zip', extend_calendar)
        self.model = EtaModel()
        self.live = LiveOverlay(self.model,self.root)
        self.router = Router(self.root/'schedule.zip',self.root,None,self.live)
        self.live.bind(self.router)
        self.when = datetime(2026,11,21,8,13,tzinfo=IST)

    def make_feed(self, receipt_offset=-3, live_offset=-3, trip='early', skip=False):
        epoch = int(self.when.timestamp())
        message = protobuf().FeedMessage()
        message.header.gtfs_realtime_version = '2.0'
        message.header.timestamp = epoch + receipt_offset-1
        item = message.entity.add(id='item').trip_update
        item.vehicle.id = 'observed-bus'
        item.trip.trip_id, item.trip.route_id, item.trip.start_date = trip, 'r2', '20261121'
        item.timestamp = message.header.timestamp
        for s,offset in [('B',live_offset),('C',live_offset+600)]:
            c = item.stop_time_update.add(stop_id=s)
            if skip and s=='B':
                c.schedule_relationship = c.SKIPPED
            else:
                c.departure.time = epoch+offset
        name = f'predictions-{epoch+receipt_offset}-{message.header.timestamp}.pb.gz'
        (self.capture/name).write_bytes(gzip.compress(message.SerializeToString()))

    def load(self, when=None):
        instant = when or self.when
        return load_november_capture(self.router,self.model,self.root,int(instant.timestamp()))

    def test_receipt_order_query_isolation_and_typed_platform_output(self):
        self.make_feed()
        self.make_feed(receipt_offset=10,live_offset=200)
        replay,meta = self.load()
        expected = self.when - timedelta(seconds=3)
        self.assertEqual(meta['received_files'],1)
        self.assertEqual(meta['last_receipt'],expected.isoformat())
        result = platform_estimate(self.router,replay,'B','r2',self.when)
        self.assertEqual(result['wait_seconds'],720)
        possible = result['possible_boarding']
        self.assertEqual((possible['trip_id'],possible['seconds_before_ready'],possible['fallback_wait_seconds']),('early',3,720))
        self.assertEqual(possible['predicted_departure'],expected.isoformat())
        self.assertIsNone(platform_estimate(self.router,self.live,'B','r2',self.when).get('possible_boarding'))
        PlatformEstimate.model_validate(dict(result,replay=meta))
        self.assertFalse(replay.eligible(int(self.when.timestamp())-2,int(self.when.timestamp())+1))
        later = self.when + timedelta(seconds=10)
        replay2,meta2 = self.load(later)
        self.assertEqual(meta2['received_files'],2)
        self.assertEqual(platform_estimate(self.router,replay2,'B','r2',later)['wait_seconds'],190)

    def test_no_feasible_fallback_or_skipped_platform(self):
        self.when = self.when.replace(minute=25)
        self.make_feed(trip='late')
        self.make_feed(receipt_offset=5,trip='late')
        overlay,_ = self.load()
        result = platform_estimate(self.router,overlay,'B','r2',self.when)
        self.assertIsNone(result['wait_seconds'])
        self.assertIsNone(result.get('possible_boarding'))
        self.when = self.when.replace(minute=13)
        self.make_feed(skip=True)
        overlay,_ = self.load()
        self.assertIsNone(platform_estimate(self.router,overlay,'B','r2',self.when).get('possible_boarding'))

    def test_recorded_downstream_passage_suppresses_only_the_identified_bus(self):
        self.make_feed()
        self.make_feed(receipt_offset=8)
        overlay,_ = self.load()
        stamp = int(self.when.timestamp())
        passed = {'route_id':'r2','stop_id':'C','position_trip':'early','service_date':'20261121','position_vehicle':'observed-bus',
                  'timestamp':stamp-2,'received_at':stamp-1}
        with patch.object(overlay,'recent_passages',return_value=[passed]):
            self.assertIsNone(platform_estimate(self.router,overlay,'B','r2',self.when).get('possible_boarding'))
            self.assertFalse(self.router.journeys('B','C',self.when,eta=overlay)['boarding_alerts'])
        with patch.object(overlay,'recent_passages',return_value=[dict(passed,position_trip='late')]):
            self.assertTrue(platform_estimate(self.router,overlay,'B','r2',self.when).get('possible_boarding'))
        with patch.object(overlay,'recent_passages',return_value=[dict(passed,position_vehicle='other')]):
            self.assertTrue(self.router.journeys('B','C',self.when,eta=overlay)['boarding_alerts'])

    def test_no_message_before_first_receipt_and_reject_outside_capture(self):
        self.make_feed(receipt_offset=5)
        self.make_feed(receipt_offset=10)
        overlay,meta = self.load()
        self.assertEqual((meta['received_files'],meta['last_receipt']),(0,None))
        self.assertIsNone(platform_estimate(self.router,overlay,'B','r2',self.when).get('possible_boarding'))
        with self.assertRaises(ValueError):
            self.load(self.when + timedelta(seconds=15))

    def test_wider_margin_honours_confirmed_downstream_passage(self):
        self.make_feed(receipt_offset=-65,live_offset=-47)
        self.make_feed(receipt_offset=8)
        overlay,_ = self.load()
        stamp = int(self.when.timestamp())
        passage = {'route_id':'r2','stop_id':'C','position_trip':'early','service_date':'20261121',
                   'position_vehicle':'observed-bus','timestamp':stamp-8,'received_at':stamp-1}
        with patch.object(overlay,'recent_passages',return_value=[passage]):
            self.assertFalse(self.router.journeys('B','C',self.when,eta=overlay)['boarding_alerts'])
            self.assertNotIn('possible_boarding',platform_estimate(self.router,overlay,'B','r2',self.when))
