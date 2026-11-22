"""Regression cases from the October development dates and midnight service boundaries."""
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest

from test_router import feed
from transit.arrivals import platform_estimate, upstream_prediction
from transit.eta import EtaModel
from transit.live import protobuf
from transit.replay import CapturedOverlay, load_november_capture
from transit.router import IST, Router


class FleetBoarding(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        def extend(tables):
            tables['trips'].append(['r1','s','fallback','shape'])
            tables['stop_times'].extend([['fallback',s,i,clock,clock] for i,s,clock in
                                         [(0,'A','00:50:00'),(1,'B','01:00:00')]])
        feed(self.root/'feed.zip',extend)
        self.model = EtaModel()
        self.router = Router(self.root/'feed.zip',self.root,None,self.model)
        self.dt = datetime(2026,10,16,0,20,tzinfo=IST)
        self.issue = int(self.dt.timestamp())
        self.day = self.dt.date()-timedelta(days=1)
        self.live = CapturedOverlay(self.model,self.root,self.issue).bind(self.router)

    def wire(self,offset):
        feed = protobuf().FeedMessage()
        feed.header.gtfs_realtime_version = '2.0'
        feed.header.timestamp = self.issue+offset
        return feed

    def forecast(self,receipt):
        message = self.wire(-20)
        bus = message.entity.add(id='call').trip_update
        bus.trip.trip_id,bus.trip.route_id,bus.trip.start_date = 'overnight','r1',self.day.strftime('%Y%m%d')
        bus.timestamp,bus.vehicle.id = self.issue-20,'same-bus'
        bus.stop_time_update.add(stop_id='A').departure.time = self.issue-3
        self.live.apply_trip_updates(message,self.issue+receipt)

    def test_late_previous_service_after_static_last_call(self):
        self.forecast(-19)
        result = self.router.journeys('A','B',self.dt,eta=self.live)
        self.assertEqual([a['trip_id'] for a in result['boarding_alerts']],['overnight'])
        alert = result['boarding_alerts'][0]
        self.assertEqual((alert['service_date'],alert['gps_age_seconds'],alert['vehicle_approaching']),
                         ('2026-10-15',None,False))
        self.assertEqual(alert['fallback_itinerary']['legs'][-1]['trip_id'],'fallback')
        possible = platform_estimate(self.router,self.live,'A','r1',self.dt)['possible_boarding']
        self.assertEqual((possible['trip_id'],possible['service_date']),('overnight','2026-10-15'))

    def test_late_delivery_of_prior_forecast_has_no_effect(self):
        self.forecast(1)
        self.assertIsNone(self.live.departure('overnight','A',self.day,self.issue,self.issue))
        self.assertFalse(self.router.journeys('A','B',self.dt,eta=self.live)['boarding_alerts'])

    def position(self,observed,service_day,lat,lon):
        message = self.wire(observed)
        v = message.entity.add(id='vehicle').vehicle
        v.vehicle.id = 'same-bus'
        v.timestamp = self.issue+observed
        v.trip.trip_id,v.trip.route_id,v.trip.start_date = 'overnight','r1',service_day.strftime('%Y%m%d')
        v.position.latitude,v.position.longitude = lat,lon
        self.live.apply_positions(message,self.issue+observed)

    def test_vehicle_reused_after_midnight_does_not_relabel_a_previous_passage(self):
        self.position(-50,self.day,28.6,77.22)
        self.position(-20,self.dt.date(),28.601,77.21)
        self.assertFalse(any(row['service_date']=='20261016' and row['stop_id']=='B'
                             for row in self.live.recent_passages(self.dt)))

    def test_previous_service_day_controls_running_time_after_midnight(self):
        calls = []
        class Model:
            def duration(self,route,a,b,service_second,scheduled,**kwargs):
                calls.append((service_second,kwargs['service_date']))
                return scheduled,'schedule'
        upstream_prediction(Model(),'r1',{'A':(('A','B'),(1,(100,)))},self.dt,
                            [{'route_id':'r1','stop_id':'A','service_date':'20261015',
                              'timestamp':self.issue-60,'received_at':self.issue-55}])
        self.assertEqual(calls,[(86400+19*60,self.day)])


class DelhiDevelopmentTrace(unittest.TestCase):
    """Observed time, received time and coordinates only; vehicle identities stay pseudonymous."""
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        cls.router = Router(root/'delhi_buses_static_gtfs_v1.zip',root/'data',None,EtaModel())
        cls.data = root/'data'

    def overlay(self,issue,trip,route,stop,samples):
        live = CapturedOverlay(EtaModel(),self.data,issue).bind(self.router)
        pid,_ = live.ids[trip]
        index = self.router.patterns[pid].stops.index(stop)
        day = datetime.fromtimestamp(issue,IST).date()
        for seen,receipt,lat,lon in samples:
            message = protobuf().FeedMessage()
            message.header.gtfs_realtime_version,message.header.timestamp = '2.0',receipt
            v = message.entity.add(id='position').vehicle
            v.vehicle.id,v.timestamp = 'bus',seen
            v.trip.trip_id,v.trip.route_id,v.trip.start_date = trip,route,day.strftime('%Y%m%d')
            v.position.latitude,v.position.longitude = lat,lon
            live.apply_positions(message,receipt)
        return live,(day,pid,index,('bus',trip),issue)

    def test_curved_shape_recognises_721up_exit(self):
        issue = 1792564469
        samples = [
            (1792564406,1792564420,28.635217666626,77.1470336914063),
            (1792564428,1792564440,28.6375122070312,77.1466522216797),
            (1792564439,1792564460,28.637716293335,77.1466369628906),
        ]
        live,args = self.overlay(issue,'2183_SHORT_11_30','2183','1433',samples)
        self.assertTrue(live.passed_platform(*args))

    def test_off_route_d5403up_keeps_departure_unresolved_and_confirms_no_approach(self):
        samples = [
            (1791603679,1791603700,28.6471195220947,77.2689514160156),
            (1791603701,1791603720,28.6449108123779,77.2692031860352),
            (1791603723,1791603740,28.643669128418,77.270378112793),
            (1791603734,1791603750,28.6437225341797,77.2707672119141),
        ]
        live,args = self.overlay(1791603758,'3214_FULL_09_10','3214','2128',samples)
        self.assertFalse(live.passed_platform(*args))
        self.assertEqual(live.boarding_position(*args),(24,False))

    def test_0118_502up_has_two_upstream_fixes_in_real_received_feeds(self):
        when = datetime(2026,11,21,1,18,tzinfo=IST)
        issue = int(when.timestamp())
        live,meta = load_november_capture(self.router,self.router.eta,self.data,issue)
        self.assertEqual(meta['received_files'],4)
        plan = self.router.journeys('2383','2386',when,eta=live)
        alert, = [a for a in plan['boarding_alerts'] if a['route']=='502UP']
        self.assertEqual((alert['seconds_before_ready'],alert['gps_age_seconds'],alert['vehicle_approaching']),(3,54,True))
        self.assertIn('conditional_itinerary',alert)
        pid,_ = live.ids[alert['trip_id']]
        i = self.router.patterns[pid].stops.index('2383')
        match = live.forecast_vehicle(when.date(),alert['trip_id'],issue)
        self.assertEqual(live.boarding_position(when.date()-timedelta(days=1),pid,i,match,issue),(None,False))
        self.assertEqual(live.boarding_position(when.date(),pid,i,('different bus',match[1]),issue),(None,False))

    def test_764up_previous_service_day_offer_at_47_seconds(self):
        when = datetime(2026,11,21,1,18,tzinfo=IST)
        live,meta = load_november_capture(self.router,self.router.eta,self.data,int(when.timestamp()))
        self.assertEqual(meta['received_files'],4)
        result = self.router.journeys('10091','10092',when,eta=live)
        alert, = [a for a in result['boarding_alerts'] if a['route']=='764UP']
        self.assertEqual((alert['seconds_before_ready'],alert['gps_age_seconds'],alert['vehicle_approaching']),
                         (47,51,True))
        self.assertEqual(alert['service_date'],'2026-11-20')
        self.assertFalse(alert['catchable'])
        self.assertEqual(alert['conditional_itinerary']['shift_seconds'],47)
        self.assertEqual(alert['fallback_itinerary']['arrival'],result['journeys'][0]['arrival'])
        possible = platform_estimate(self.router,live,'10091','2232',when)['possible_boarding']
        self.assertEqual((possible['trip_id'],possible['seconds_before_ready']), (alert['trip_id'],47))

        later = when + timedelta(seconds=15)
        updated,meta = load_november_capture(self.router,self.router.eta,self.data,int(later.timestamp()))
        self.assertEqual(meta['received_files'],6)
        issue = int(when.timestamp())
        pid,_ = live.ids[alert['trip_id']]
        path = self.router.platform_path(pid,self.router.patterns[pid].stops.index('10091'))
        vehicle,_ = live.forecast_vehicle(when.date()-timedelta(days=1),alert['trip_id'],issue)
        for feed,receipt_delta,observation_delta,metres in ((live,-15,-51,-53.1),(updated,15,-29,174.2)):
            _,received,vehicles = feed.vehicle_witnesses
            _,_,_,observed,position,_ = vehicles[vehicle]
            self.assertEqual((received-issue,observed-issue),(receipt_delta,observation_delta))
            self.assertAlmostEqual(path.project(position,45),metres,delta=0.1)
        updated_plan = self.router.journeys('10091','10092',later,eta=updated)
        self.assertFalse(any(a['trip_id']==alert['trip_id'] for a in updated_plan['boarding_alerts']))

    def test_distant_measures_on_two_nearby_shape_branches_remain_unassigned(self):
        from transit.boarding import PlatformPath
        points = [(28.6,77.2),(28.6,77.21),(28.60005,77.21),(28.60005,77.2),(28.60005,77.19)]
        path = PlatformPath.build(points,[0,3,4],1,points[3])
        self.assertIsNone(path.project((28.600025,77.205)))
