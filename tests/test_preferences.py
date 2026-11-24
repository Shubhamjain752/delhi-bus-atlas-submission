import unittest

from transit.preferences import balanced_selection


class JourneyPreferenceTests(unittest.TestCase):
    def test_ten_percent_uses_unrounded_door_to_door_seconds(self):
        for faster, leader in [(1799, 0), (1800, 0), (1801, 1)]:
            with self.subTest(faster=faster):
                order, policy = balanced_selection([(faster, 500, 1), (2000, 200, 1)], 5)
                self.assertEqual(order[0], leader)
                self.assertEqual(policy["primary_count"], 2)
                self.assertEqual(order[policy["faster_index"]], 0)

    def test_five_minutes_defines_longer_walk_even_on_a_direct_bus(self):
        for extra, leader in [(299, 0), (300, 1)]:
            order, _ = balanced_selection([(1870, 500, 0), (2000, 500-extra, 1)], 5)
            self.assertEqual(order[0], leader)

    def test_vivek_keeps_original_three_and_one_earlier_walk(self):
        # The actual 10:57 candidates. The 473 to the 85 and 85EXT both
        # take longer, with more walking, than the 473 to the 317.
        choices = [(4556, 1325, 1), (4667, 1486, 1), (4949, 1486, 1),
                   (5019, 635, 1), (5019, 817, 1), (5019, 1180, 1),
                   (5180, 635, 1), (5240, 901, 1), (15000, 5, 0)]
        order, policy = balanced_selection(choices, 5)
        self.assertEqual(order, [3, 4, 6, 0, 7])
        self.assertEqual(policy, {"primary_count": 3, "faster_index": 3, "less_walking_index": 0,
                                  "time_saved_seconds": 463, "extra_walk_seconds": 690})

    def test_no_pointless_longer_walk_to_fill_spare_slots(self):
        choices = [(2000, 1200, 1), (2001, 150, 1), (2010, 140, 1), (2800, 1250, 0)]
        self.assertEqual(balanced_selection(choices, 5), ([1, 2], {"primary_count": 2}))
        for saving, included in [(119, False), (120, True)]:
            order, _ = balanced_selection([(2000-saving, 1200, 1), (2000, 150, 1)], 5)
            self.assertEqual(0 in order, included)
        order, _ = balanced_selection([(810, 420, 1), (900, 100, 1)], 5)
        self.assertEqual(order, [0, 1])  # Exactly 10% on a shorter journey.

    def test_later_walks_do_not_displace_three_nearby_short_options(self):
        choices = [(2000, 600, 1), (2100, 600, 1), (2900, 600, 1), (2901, 0, 0)]
        self.assertEqual(balanced_selection(choices, 5), ([0, 1, 2], {"primary_count": 3}))

    def test_short_walk_backups_on_infrequent_routes(self):
        # Adhchini at night. The second service has the same short walk.
        # The third is a later backup with eight minutes of walking.
        # A similar fourth service is unnecessary after filling three places.
        choices = [(12915, 76, 1), (14163, 76, 1), (14247, 485, 1), (14255, 485, 1)]
        self.assertEqual(balanced_selection(choices, 5), ([0, 1, 2], {"primary_count": 3}))

        # The absolute walk cap includes exactly 15 minutes.
        choices = [(6000, 90, 0), (7200, 900, 1), (7201, 901, 1), (8800, 100, 1)]
        self.assertEqual(balanced_selection(choices, 5), ([0, 1], {"primary_count": 2}))
        # Punjabi Bagh. The quicker trip must still be visible when a
        # lower-walk group starts more than half an hour later.
        choices = [(4687, 1308, 1), (4742, 1308, 1),
                   (6733, 682, 1), (6733, 682, 1), (6760, 682, 1)]
        order, policy = balanced_selection(choices, 5)
        self.assertEqual(order, [0, 2, 3, 4])
        self.assertEqual(policy["faster_index"], 0)

    def test_backups_stop_at_half_the_trip_bounded_by_30_and_45_minutes(self):
        for fastest, allowance in [(600, 1800), (4000, 2000), (10000, 2700)]:
            with self.subTest(fastest=fastest):
                choices = [(fastest, 100, 1), (fastest+allowance, 100, 1),
                           (fastest+allowance+1, 0, 0)]
                self.assertEqual(balanced_selection(choices, 5), ([0, 1], {"primary_count": 2}))

    def test_two_earlier_long_walks_must_offer_a_distinct_advantage(self):
        choices = [(1700, 1200, 1), (1710, 1195, 1), (1750, 900, 1),
                   (1770, 990, 1), (1800, 800, 0),
                   (2000, 400, 1), (2010, 440, 1), (2020, 410, 1)]
        order, policy = balanced_selection(choices, 5)
        self.assertEqual(order, [0, 5, 6, 7, 2])
        self.assertEqual(policy["faster_index"], 0)
        self.assertEqual(policy["primary_count"], 3)

    def test_rohini_demo_adds_a_moderate_walk_after_the_two_main_choices(self):
        choices = [(4199, 1042, 1), (4343, 1155, 1), (4389, 317, 0), (5037, 642, 1),
                   (5332, 954, 1), (8900, 253, 0)]
        self.assertEqual(balanced_selection(choices, 5)[0], [2, 0, 3])

    def test_represent_convenient_direct_journey_alongside_transfers(self):
        choices = [(1000, 240, 1), (1010, 250, 1), (1020, 270, 1), (1080, 260, 0)]
        order, _ = balanced_selection(choices, 5)
        self.assertEqual(order, [0, 1, 3, 2])

    def test_later_direct_backup_does_not_preempt_nearby_short_walks(self):
        choices = [(1650, 750, 1), (1900, 300, 1), (1910, 320, 1), (3000, 310, 0)]
        self.assertEqual(balanced_selection(choices, 5)[0], [0, 1, 2, 3])

    def test_no_walk_comparison_badge_for_a_one_second_difference(self):
        # Relative to the shortest walk, these are in different groups.
        # The two fastest options themselves differ by only one second on foot.
        choices = [(1800, 400, 1), (2040, 399, 1), (2100, 100, 1)]
        order, policy = balanced_selection(choices, 5)
        self.assertEqual(order, [0, 1, 2])
        self.assertEqual(policy, {"primary_count": 3})

    def test_return_no_more_than_requested_with_correct_comparison_indices(self):
        values = [(1870, 500, 1), (2000, 200, 1)]
        self.assertEqual(balanced_selection(values, 1), ([1], {
            "primary_count": 1, "less_walking_index": 0, "time_saved_seconds": 130, "extra_walk_seconds": 300}))
        self.assertEqual(balanced_selection([], 5), ([], {"primary_count": 0}))
        for count in range(1, 7):
            choices = [(1000+i*50, 500+i, 1) for i in range(count)]
            for limit in range(1, 7):
                with self.subTest(available=count, requested=limit):
                    order, policy = balanced_selection(choices, limit)
                    self.assertEqual(order, list(range(min(count, limit, 5))))
                    self.assertEqual(policy["primary_count"], min(count, limit, 3))
