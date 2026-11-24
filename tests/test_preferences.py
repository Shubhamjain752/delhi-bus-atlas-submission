import unittest

from transit.preferences import balanced_selection


class JourneyPreferenceTests(unittest.TestCase):
    def test_ten_percent_uses_unrounded_door_to_door_seconds(self):
        for faster, leader in [(899, 0), (900, 0), (901, 1)]:
            with self.subTest(faster=faster):
                order, policy = balanced_selection([(faster, 500, 1), (1000, 200, 1)], 5)
                self.assertEqual(order[0], leader)
                self.assertEqual(policy["primary_count"], 2)
                self.assertEqual(order[policy["faster_index"]], 0)

    def test_five_minutes_walking_and_no_extra_change(self):
        for extra, preferred in [(299, 0), (300, 1)]:
            order, _ = balanced_selection([(950, 350, 1), (1000, 350-extra, 1)], 5)
            self.assertEqual(order[0], preferred)
        order, policy = balanced_selection([(950, 350, 0), (1000, 50, 1)], 5)
        self.assertEqual(order[0], 0)
        self.assertNotIn("less_walking_index", policy)

    def test_first_practical_alternative_keeps_slow_low_walk_option_from_leading(self):
        order, policy = balanced_selection([(4556,1325,1), (4667,1486,1), (4949,1486,1),
                                           (5019,635,1), (15000,5,0)], 5)
        self.assertEqual(order[:3], [3, 0, 4])
        self.assertEqual((policy["time_saved_seconds"],policy["extra_walk_seconds"]), (463,690))
        self.assertEqual(order[3:], [1,2])

    def test_one_visible_journey_does_not_overflow_limit(self):
        values = [(950, 350, 1), (1000, 50, 1)]
        self.assertEqual(balanced_selection(values, 1), ([1], {
            "primary_count": 1, "less_walking_index": 0, "time_saved_seconds": 50, "extra_walk_seconds": 300}))
        self.assertEqual(balanced_selection([], 5), ([], {"primary_count":0}))
        similar = [(1000+i*50, 500+i, 1) for i in range(10)]
        order, policy = balanced_selection(similar, 5)
        self.assertEqual(order, [0,1,2])
        self.assertEqual(policy["primary_count"], 1)

    def test_three_competitive_lower_walk_choices_put_fastest_under_more(self):
        choices = [(900, 1000, 1), (920, 650, 1), (925, 675, 1),
                   (940, 600, 1), (960, 640, 0), (970, 650, 1)]
        order, policy = balanced_selection(choices, 5)
        self.assertEqual(order,[1,3,4,0,2])
        self.assertEqual(policy["primary_count"],3)
        self.assertEqual(policy["faster_index"],3)
        self.assertEqual(policy["less_walking_index"],0)

    def test_two_useful_choices_both_show_even_when_there_are_similar_routes(self):
        order, policy = balanced_selection([(4556,1325,1), (4667,1486,1),
                                           (4949,1486,1), (5019,635,1)], 5)
        self.assertEqual(order[:2], [3,0])
        self.assertEqual(policy["primary_count"],2)
