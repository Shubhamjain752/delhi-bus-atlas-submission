"""The historical origin classifier must not mistake passing buses for launched runs."""
from datetime import date
from types import SimpleNamespace
import unittest

from tools.fit_origin_service import edge_key, midnight, name, origin_index, schedule


def pattern(stops, pickups, trip="run", start=0):
    return SimpleNamespace(route="1", service="all", stops=stops, pickups=pickups,
                           starts=[start], trips=[trip], departures=[60*i for i in range(len(stops))])


def network(*patterns):
    return SimpleNamespace(patterns=patterns, _active=lambda day:{"all"},
                           stops={str(i):{"lat":28.7, "lon":77+i/100} for i in range(5)})


class OriginServiceFitTests(unittest.TestCase):
    def test_large_real_stop_ids_round_trip_without_aliasing(self):
        self.assertEqual(name(edge_key("852","1374322","8595906"),8),"852|1374322|8595906|weekday|8")
        self.assertNotEqual(edge_key("852","7567","1374322"),edge_key("852","7568","325746"))
        with self.assertRaises(ValueError):
            edge_key("1","2",1<<24)

    def test_first_public_pickup_can_follow_private_call(self):
        router=network(pattern(["0","1","2"],[1,0,0]))
        origin,platform=origin_index(router,{"all"})
        self.assertEqual(origin,{edge_key("1","1","2")})
        self.assertEqual(platform,origin)

    def test_overlapping_pattern_disqualifies_start_but_keeps_directed_platform(self):
        router=network(pattern(["1","2"],[0,0]),pattern(["0","1","2"],[0,0,0]))
        origin,platform=origin_index(router,{"all"})
        self.assertNotIn(edge_key("1","1","2"),origin)
        self.assertIn(edge_key("1","1","2"),platform)

    def test_overlapping_stop_areas_are_ambiguous(self):
        router=network(pattern(["1","2"],[0,0]))
        router.stops["1"]["lon"]=router.stops["2"]["lon"]+.0001
        self.assertEqual(origin_index(router,{"all"}),(set(),set()))

    def test_previous_service_day_run_counted_once_on_civil_date(self):
        day=date(2026,10,3)
        router=network(pattern(["1","2"],[0,0],start=86400+180))
        self.assertEqual(list(schedule(router,day)),[("1","1","2",midnight(day)+180,"run")])
