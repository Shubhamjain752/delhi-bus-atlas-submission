from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_disruptions import add_services
from test_router import feed
from transit.eta import EtaModel
from transit.live import LiveOverlay, protobuf
from transit.router import IST, Router, _Query


class VehicleRuns(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name)
        feed(path / 'feed.zip', add_services)
        self.router = Router(path / 'feed.zip', path, None, None)
        self.overlay = LiveOverlay(EtaModel(), path).bind(self.router)
        self.router.eta = self.overlay
        self.dt = datetime(2026, 10, 15, 8, 1, tzinfo=IST)
        self.now = int(self.dt.timestamp())
        clock = patch('transit.live.time.time',return_value=self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def wire(self):
        f = protobuf().FeedMessage()
        f.header.gtfs_realtime_version = '2.0'
        f.header.timestamp = self.now
        return f

    def apply(self, rows, vehicles=('one','two'), offset=0, receipt_offset=0, position_date='20261015',
              same_direction=True):
        positions = self.wire()
        updates = self.wire()
        for vid in vehicles:
            v = positions.entity.add(id=vid).vehicle
            v.vehicle.id = vid
            v.trip.trip_id, v.trip.route_id, v.trip.start_date = 'bus0','line',position_date
            v.trip.direction_id = 0 if same_direction else 1
            v.timestamp = self.now + offset
            v.position.latitude, v.position.longitude = 28.61,77.4
        for vid,calls in rows:
            u = updates.entity.add(id=str(len(updates.entity))).trip_update
            u.vehicle.id = vid
            u.trip.trip_id, u.trip.route_id, u.trip.start_date = 'bus0','line','20261015'
            u.trip.direction_id = 0
            u.timestamp = self.now
            if calls is None:
                u.trip.schedule_relationship = u.trip.CANCELED
            for stop,seconds in calls or ():
                c = u.stop_time_update.add(stop_id=stop)
                if seconds is None:
                    c.schedule_relationship = c.SKIPPED
                else:
                    c.departure.time = self.now + seconds
        self.overlay.apply_trip_updates(updates,self.now)
        self.overlay.apply_positions(positions,self.now + receipt_offset,provider='community_positions')

    def journey(self,origin='X',dest='Z'):
        result = self.router.journeys(origin,dest,self.dt,max_walk_m=0,max_transfers=0)
        return next(l for l in result['journeys'][0]['legs'] if l['mode']=='bus')

    first = ('one', [('X',30),('Y',300),('Z',600)])
    second = ('two', [('X',100),('Y',260),('Z',500)])

    def test_keeps_the_vehicle_together_even_when_it_overtakes(self):
        self.apply([self.first,self.second])
        ride = self.journey()
        self.assertEqual(ride['seconds'],400)
        self.assertEqual(ride['trip_id'],'bus0')
        self.assertTrue(ride['live_run_id'])
        self.assertNotIn('two',ride['live_run_id'])
        self.assertEqual([datetime.fromisoformat(c['arrival']).timestamp()-self.now for c in ride['calls']], [100,260,500])
        p = next(p for p in self.router.patterns if 'bus0' in p.trips)
        q = _Query(self.router,self.dt,{})
        choices = q.next_rides(self.router.patterns.index(p),0,q.start)
        self.assertEqual(len([r for r in choices if r.trip=='bus0']),2)
        self.assertTrue(all(r.live_run_id for r in choices if r.trip=='bus0'))

    def test_unconfirmed_or_conflicting_ids_use_one_static_fallback(self):
        for rows,vehicles,offset in (([self.first,self.second],['one'],0),([self.first,self.first,self.second],['one','two'],0),
                                     ([self.first,self.second],['one','two'],-300)):
            with self.subTest(vehicles=vehicles,offset=offset):
                self.apply(rows,vehicles,offset)
                self.assertFalse(self.journey().get('live_run_id'))
        for options in ({'receipt_offset':5}, {'position_date':'20261014'}, {'same_direction':False}):
            with self.subTest(options=options):
                self.apply([self.first,self.second],**options)
                self.assertFalse(self.journey().get('live_run_id'))

    def test_vehicle_cannot_borrow_calls_from_another_run(self):
        self.apply([('one',[('X',30),('Y',300)]),('two',[('Y',260),('Z',500)])])
        self.assertNotEqual(self.journey()['trip_id'],'bus0')
        self.assertTrue(self.journey('X','Y')['live_run_id'])
        self.assertTrue(self.journey('Y','Z')['live_run_id'])

    def test_conditional_and_fallback_keep_each_physical_run_intact(self):
        self.apply([('one',[('X',-3),('Y',150),('Z',300)]), ('two',[('X',200),('Y',500),('Z',900)])])
        result = self.router.journeys('X','Z',self.dt,max_walk_m=0,max_transfers=0)
        alert, = result['boarding_alerts']
        conditional = next(l for l in alert['conditional_itinerary']['legs'] if l['mode']=='bus')
        self.assertEqual(conditional['live_run_id'],alert['live_run_id'])
        self.assertEqual(conditional['calls'][-1]['arrival'],datetime.fromtimestamp(self.now + 303,IST).isoformat())
        fallback = next(l for l in alert['fallback_itinerary']['legs'] if l['mode']=='bus')
        self.assertNotEqual(fallback.get('live_run_id'),conditional['live_run_id'])

    def test_canceled_or_skipped_vehicle_does_not_block_its_sibling(self):
        for unavailable in (('one',None),('one',[('X',30),('Y',300),('Z',None)])):
            self.apply([unavailable,self.second])
            self.assertEqual(self.journey()['seconds'],400)
            self.assertTrue(self.journey()['live_run_id'])

    def test_unassigned_cancel_and_physical_vehicle_skip_block_access(self):
        self.apply([('',None),self.second])
        self.assertNotEqual(self.journey()['trip_id'],'bus0')
        self.apply([('one',[('X',30),('Y',None),('Z',600)]),self.second])
        skipped = self.journey('X','Y')
        self.assertEqual(skipped['seconds'],160)
        anchor = {'route_id':'line','stop_id':'X','position_trip':'bus0','position_vehicle':'one','service_date':'20261015'}
        self.assertFalse(self.overlay.can_approach(anchor,'Y',self.dt))
        self.assertTrue(self.overlay.can_approach(dict(anchor,position_vehicle='two'),'Y',self.dt))
