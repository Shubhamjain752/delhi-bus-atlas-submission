from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from transit.arrivals import platform_estimate, upstream_prediction
from transit.contract import PlatformEstimate
from transit.eta import EtaModel, IST
from transit.live import LiveOverlay
from transit.router import Router
from test_disruptions import add_services
from test_router import feed


class PlatformArrivals(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        directory = Path(self.folder.name)
        feed(directory/"feed.zip",add_services)
        self.when = datetime(2026,10,15,8,2,tzinfo=IST)
        self.issue = int(self.when.timestamp())
        self.model = EtaModel()
        self.overlay = LiveOverlay(self.model,directory)
        self.router = Router(directory/"feed.zip",directory,None,self.overlay)
        self.overlay.bind(self.router)
        self.seen = {"route_id":"line","stop_id":"X","timestamp":self.issue-45,"received_at":self.issue-5}

    def tearDown(self):
        self.folder.cleanup()

    def test_anchored_platform_result_is_independent_of_anonymous_trip_identity(self):
        with patch.object(self.overlay,"recent_passages",return_value=[self.seen]):
            r = platform_estimate(self.router,self.overlay,"Y","line",self.when)
        PlatformEstimate.model_validate(r)
        self.assertEqual(r["source"],"upstream_gps")
        self.assertEqual(r["wait_seconds"],195)
        self.assertEqual(r["observed_stop"]["id"],"X")

    def test_future_late_and_stale_evidence_and_terminal_excluded(self):
        path = {"X":(("X","Y"),(1,(240,)))}
        for changes in ({"received_at":self.issue+1},{"timestamp":self.issue-181},
                        {"route_id":"other"},{"stop_id":"Y"}):
            self.assertIsNone(upstream_prediction(self.model,"line",path,self.when,[dict(self.seen,**changes)]))
        with self.assertRaisesRegex(ValueError,"boardable"):
            platform_estimate(self.router,self.overlay,"Z","line",self.when)

    def test_known_cancelled_or_skipped_trip_cannot_generate_platform_anchor(self):
        seen = dict(self.seen, position_trip="bus0")
        key = self.when.date(), "bus0"
        for restriction in (None,frozenset([1])):
            with patch.object(self.overlay,"trip_exclusions",return_value={key:restriction}):
                self.assertFalse(self.overlay.can_approach(seen,"Y",self.when))
        self.assertTrue(self.overlay.can_approach(seen,"Y",self.when))
