from datetime import datetime
import tempfile
from pathlib import Path
import unittest

from test_router import feed
from transit.eta import EtaModel, IST
from transit.live import protobuf
from transit.arrivals import platform_estimate
from transit.replay import CapturedOverlay
from transit.router import Router


class ConfirmedPlatformExit(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name)
        feed(self.data/'s.zip')
        self.dt = datetime(2026,10,15,8,13,tzinfo=IST)
        self.now = int(self.dt.timestamp())
        self.router = Router(self.data/'s.zip',self.data,None,EtaModel())

    def messages(self,positions):
        live = CapturedOverlay(EtaModel(),self.data,self.now).bind(self.router)
        wire = protobuf().FeedMessage()
        wire.header.gtfs_realtime_version='2.0'
        wire.header.timestamp=self.now-20
        u=wire.entity.add(id='eta').trip_update
        u.timestamp=self.now-20
        u.vehicle.id='known'
        u.trip.trip_id,u.trip.route_id,u.trip.start_date='early','r2','20261015'
        u.stop_time_update.add(stop_id='B').departure.time=self.now-3
        live.apply_trip_updates(wire,self.now-20)
        for ts,lat,lon,changes in positions:
            wire=protobuf().FeedMessage()
            wire.header.gtfs_realtime_version='2.0'
            receipt=self.now+changes.get('receipt',ts)
            wire.header.timestamp=receipt
            v=wire.entity.add(id='gps').vehicle
            v.timestamp=self.now+ts
            v.vehicle.id=changes.get('vehicle','known')
            v.trip.trip_id,v.trip.route_id,v.trip.start_date='early','r2',changes.get('date','20261015')
            v.position.latitude,v.position.longitude=lat,lon
            if changes.get('conflict'):
                other=wire.entity.add(id='duplicate').vehicle
                other.CopyFrom(v)
                other.position.longitude+=0.01
            live.apply_positions(wire,receipt,provider='community_positions')
        return live

    def results(self,entries):
        near=[(-55,28.6,77.2187,{}),(-25,28.6,77.21995,{})]
        overlay=self.messages(near+entries)
        plan=self.router.journeys('B','C',self.dt,eta=overlay)
        platform=platform_estimate(self.router,overlay,'B','r2',self.dt)
        return bool(plan['boarding_alerts']),bool(platform.get('possible_boarding'))

    def test_forward_exit_vetoes_both_endpoints_but_is_not_backdated(self):
        self.assertEqual(self.results([(-5,28.6,77.2208,{})]),(False,False))
        self.assertEqual(self.results([(-5,28.6,77.2208,{'receipt':2})]),(True,True))

    def test_no_veto_from_backward_off_route_fast_conflicting_or_other_vehicle(self):
        for point in [(-5,28.6,77.219,{}),(-5,28.602,77.2208,{}),(-24,28.6,77.2215,{}),
                      (-5,28.6,77.2208,{'vehicle':'other'}),(-5,28.6,77.2208,{'date':'20261016'}),
                      (-5,28.6,77.2208,{'conflict':True})]:
            with self.subTest(point=point):
                self.assertEqual(self.results([point]),(True,True))

    def test_return_to_platform_clears_prior_exit(self):
        self.assertEqual(self.results([(-12,28.6,77.2208,{}),(-3,28.6,77.22,{})]),(True,True))

    def test_stale_or_discontinuous_observations_cannot_prove_departure(self):
        for times in ((-230,-210,-190),(-120,-80,-3)):
            with self.subTest(times=times):
                entries=[(t,28.6,lon,{}) for t,lon in zip(times,(77.2187,77.21995,77.2208))]
                overlay=self.messages(entries)
                self.assertTrue(self.router.journeys('B','C',self.dt,eta=overlay)['boarding_alerts'])
