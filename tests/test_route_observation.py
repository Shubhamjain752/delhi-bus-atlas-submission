from datetime import datetime
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_router import feed
from transit.api import app,arrivals
from transit.arrivals import platform_estimate
from transit.eta import EtaModel
from transit.live import protobuf
from transit.replay import CapturedOverlay
from transit.route_observation import ObservationOverlay,PlatformResponse,route_observation
from transit.router import IST,Router


class ObservedCapture(CapturedOverlay,ObservationOverlay):
    pass


class UnassignedRouteObservation(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder=Path(temp.name)
        self.when=datetime(2026,10,15,8,0,tzinfo=IST)
        self.issue=int(self.when.timestamp())
        self.network()

    def network(self,branch=False):
        def edit(t):
            t['routes'].append(['rs','S','DTC'])
            t['trips'].append(['rs','s','rs-official','rs-shape'])
            t['shapes'].extend([['rs-shape',28.6,77.20,0],['rs-shape',28.6,77.22,1],['rs-shape',28.6,77.24,2]])
            t['stop_times'].extend([['rs-official',s,i,clock,clock] for i,s,clock in
                                    [(0,'A','08:00:00'),(1,'B','08:10:00'),(2,'C','08:20:00')]])
            if branch:
                t['trips'].append(['rs','s','before-target','rs-shape'])
                t['stop_times'].extend([['before-target','A',0,'08:15:00','08:15:00'],
                                        ['before-target','C',1,'08:35:00','08:35:00']])
        path=self.folder/('branch.zip' if branch else 'feed.zip')
        feed(path,edit)
        self.router=Router(path,self.folder,None,EtaModel())
        self.live=ObservedCapture(self.router.eta,self.folder,self.issue).bind(self.router)

    def message(self,when):
        m=protobuf().FeedMessage()
        m.header.gtfs_realtime_version,m.header.timestamp='2.0',self.issue+when
        return m

    def position(self,stamp,lon,receipt=None,day='20261015',vehicle='physical',trip='rs_07_57_1234',duplicate=False):
        received=stamp if receipt is None else receipt
        msg=self.message(received)
        v=msg.entity.add(id='gps').vehicle
        v.vehicle.id,v.timestamp=vehicle,self.issue+stamp
        v.trip.route_id,v.trip.trip_id,v.trip.start_date='rs',trip,day
        v.position.latitude,v.position.longitude=28.6,lon
        if duplicate:
            other=msg.entity.add(id='other').vehicle
            other.CopyFrom(v)
        self.live.apply_positions(msg,self.issue+received)

    def answer(self):
        ordinary=platform_estimate(self.router,self.live,'B','rs',self.when)
        return ordinary,route_observation(self.router,self.live,'B','rs',self.when,ordinary)

    def test_http_adds_movement_without_changing_timetable_or_journeys(self):
        self.position(-32,77.19978,-27)
        self.position(-12,77.20035,-7)
        ordinary,seen=self.answer()
        self.assertEqual((seen['observed_stop']['id'],seen['age_seconds'],seen['trip_assignment']),
                         ('A',12,'unmatched_start_clock'))
        planner=SimpleNamespace(live=self.live,router=self.router)
        with patch.object(app.state,'planner',planner,create=True):
            response=arrivals('B','rs',self.when.isoformat(),None)
        payload=PlatformResponse.model_validate(response).model_dump(mode='json')
        self.assertEqual((payload['source'],payload['wait_seconds']),(ordinary['source'],ordinary['wait_seconds']))
        self.assertEqual(payload['route_observation']['direction'],'toward_platform')
        self.assertNotIn('arrival',payload['route_observation'])
        self.assertNotIn('trip_id',payload['route_observation'])
        self.assertFalse(self.router.journeys('B','C',self.when,eta=self.live)['boarding_alerts'])

    def test_stronger_evidence_or_contradictory_active_pattern_abstains(self):
        self.position(-32,77.19978)
        self.position(-12,77.20035)
        for stronger in [{'source':'live'},{'source':'upstream_gps'},{'source':'timetable','possible_boarding':{'x':1}}]:
            self.assertIsNone(route_observation(self.router,self.live,'B','rs',self.when,stronger))
        self.network(branch=True)
        self.position(-32,77.19978)
        self.position(-12,77.20035)
        self.assertIsNone(self.answer()[1])

    def test_stale_late_wrong_direction_or_competing_identity_abstains(self):
        for first,second in [
            ((-100,77.19978,{}),(-70,77.20035,{})),((-32,77.19978,{}),(-12,77.20035,{'receipt':2})),
            ((-32,77.20035,{}),(-12,77.19978,{})),((-32,77.19978,{}),(-12,77.20035,{'vehicle':'other'})),
            ((-32,77.19978,{}),(-12,77.20035,{'day':'20261014'})),
            ((-32,77.19978,{}),(-12,77.20035,{'duplicate':True})),
            ((-32,77.19978,{}),(-12,77.20035,{'trip':'rs_08_00_1234'}))]:
            with self.subTest(first=first,second=second):
                self.live=ObservedCapture(self.router.eta,self.folder,self.issue).bind(self.router)
                for t,x,kw in (first,second):
                    self.position(t,x,**kw)
                self.assertIsNone(self.answer()[1])

    def test_same_raw_cancellation_and_route_skip_suppress_only_advisory(self):
        for raw,cancel,stop in [('rs_07_57_1234',True,None),('rs-official',False,'B')]:
            with self.subTest(raw=raw):
                self.live=ObservedCapture(self.router.eta,self.folder,self.issue).bind(self.router)
                self.position(-32,77.19978)
                self.position(-12,77.20035)
                msg=self.message(-5)
                u=msg.entity.add(id='restriction').trip_update
                u.trip.route_id,u.trip.trip_id,u.trip.start_date='rs',raw,'20261015'
                if cancel:
                    u.trip.schedule_relationship=u.trip.CANCELED
                else:
                    call=u.stop_time_update.add(stop_id=stop)
                    call.schedule_relationship=call.SKIPPED
                self.live.apply_trip_updates(msg,self.issue-5)
                self.assertIsNone(self.answer()[1])
