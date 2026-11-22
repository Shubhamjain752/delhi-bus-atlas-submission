from datetime import datetime
from pathlib import Path
import tempfile
import unittest

from test_router import feed
from transit.arrivals import platform_estimate
from transit.eta import EtaModel
from transit.live import LiveOverlay, protobuf
from transit.replay import CapturedOverlay
from transit.router import IST, Router


class DelhiProviderClock(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name)
        self.dt = datetime(2026,10,15,8,13,tzinfo=IST)
        self.now = int(self.dt.timestamp())
        self.setup_network()

    def setup_network(self,ambiguous=False):
        def extend(t):
            if ambiguous:
                t['trips'].append(['r2','s','same-minute',''])
                t['stop_times'].extend([['same-minute',s,i,clock,clock] for i,s,clock in
                                        [(0,'B','08:13:00'),(1,'C','08:23:00')]])
        name = self.path/('duplicate.zip' if ambiguous else 'feed.zip')
        feed(name,extend)
        self.router = Router(name,self.path,None,EtaModel())
        self.live = CapturedOverlay(self.router.eta,self.path,self.now).bind(self.router)

    def message(self,t):
        msg = protobuf().FeedMessage()
        msg.header.gtfs_realtime_version='2.0'
        msg.header.timestamp=self.now+t
        return msg

    def updates(self,raw='r2_08_13_0123',day='20261015',stops=('B',),start='',other_vehicle=False):
        msg = self.message(-20)
        for vehicle in ('one','two') if other_vehicle else ('one',):
            u = msg.entity.add(id=vehicle).trip_update
            u.timestamp=self.now-20
            u.vehicle.id=vehicle
            u.trip.trip_id,u.trip.route_id,u.trip.start_date,u.trip.start_time=raw,'r2',day,start
            for i,s in enumerate(stops):
                u.stop_time_update.add(stop_id=s).departure.time=self.now-3+600*i
        return self.live.apply_trip_updates(msg,self.now-20)

    def positions(self,raw):
        for t,lon in [(-55,77.2187),(-25,77.21995),(-5,77.2208)]:
            msg = self.message(t)
            v=msg.entity.add(id='gps').vehicle
            v.timestamp,v.vehicle.id=self.now+t,'one'
            v.trip.route_id,v.trip.trip_id,v.trip.start_date='r2',raw,'20261015'
            v.position.latitude,v.position.longitude=28.6,lon
            self.live.apply_positions(msg,self.now+t)

    def test_raw_suffix_matches_the_dated_stop_call_and_keeps_physical_identity(self):
        raw = 'r2_8_13_0123'
        self.assertEqual(self.updates(raw)['matched_trips'],1)
        self.assertEqual(self.live.forecast_vehicle(self.dt.date(),'early',self.now),('one',raw))
        self.assertEqual(self.router.journeys('B','C',self.dt,eta=self.live)['boarding_alerts'][0]['trip_id'],'early')
        self.positions('r2_8_13_0124')
        self.assertIsNotNone(platform_estimate(self.router,self.live,'B','r2',self.dt).get('possible_boarding'))
        self.live = CapturedOverlay(self.router.eta,self.path,self.now).bind(self.router)
        self.updates(raw)
        self.positions(raw)
        self.assertFalse(self.router.journeys('B','C',self.dt,eta=self.live)['boarding_alerts'])

    def test_legacy_explicit_clock_exact_trip_and_invalid_descriptors(self):
        for raw,start in [('early',''),('r2_08_13',''),('r2_08_13_0',''),('opaque','08:13:00'),
                          ('r2_08_12_3','08:13:00')]:
            with self.subTest(raw=raw):
                self.assertEqual(self.updates(raw,start=start)['matched_trips'],1)
        for raw,start in [('r1_08_13_1',''),('r2_08_13_',''),('r2_08_13_12345',''),
                          ('r2_08_13_1_2',''),('r2_08_13_x',''),('r2_08_99_1',''),
                          ('r2_08_13_1','08:13:72'),('opaque','07:73:00'),('r1_08_13_1','08:13:00')]:
            with self.subTest(raw=raw,start=start):
                self.assertEqual(self.updates(raw,start=start)['matched_trips'],0)
        self.assertEqual(self.updates(day='20261017')['matched_trips'],0)
        self.assertEqual(self.updates(stops=('C','B'))['matched_trips'],0)

    def test_competing_vehicles_or_static_trips_do_not_create_a_single_assignment(self):
        self.updates(other_vehicle=True)
        self.assertIsNone(self.live.forecast_vehicle(self.dt.date(),'early',self.now))
        self.assertFalse(self.router.journeys('B','C',self.dt,eta=self.live)['boarding_alerts'])
        self.setup_network(ambiguous=True)
        self.assertEqual(self.updates()['matched_trips'],0)
        passage = {'route_id':'r2','stop_id':'B','position_trip':'r2_08_13_2','service_date':'20261015'}
        self.assertFalse(self.live.can_approach(passage,'C',self.dt))

    def test_position_only_clock_requires_matching_route_and_served_pattern(self):
        passage = {'route_id':'r2','stop_id':'B','position_trip':'r2_08_13_2','service_date':'20261015'}
        self.assertTrue(self.live.can_approach(passage,'C',self.dt))
        for changes in [{'position_trip':'r1_08_13_2'},{'position_trip':'r2_08_13_unk'},
                        {'service_date':'invalid'},{'service_date':'20261017'}]:
            with self.subTest(changes=changes):
                self.assertFalse(self.live.can_approach(dict(passage,**changes),'C',self.dt))
        self.assertFalse(self.live.can_approach(passage,'A',self.dt))
        self.assertFalse(self.live.can_approach(dict(passage,position_trip='opaque',position_start_time='08:13:00'),'A',self.dt))
