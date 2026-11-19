"""Unit tests for the multi-player matchmaking rules (no Redis, no server).

Run with matcha's own venv from the repo root:
    .venv/bin/python -m unittest discover -s tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.helpers.config import config  # noqa: E402
from app.worker import _rank_window, _within, _waited_seconds  # noqa: E402


class RankWindowTest(unittest.TestCase):
    def test_thin_pool_never_filters_on_rank(self):
        # KiX's rule: two players in the queue race each other whatever their rank.
        self.assertIsNone(_rank_window(0, True))
        self.assertIsNone(_rank_window(400, True))

    def test_busy_pool_starts_narrow_and_widens(self):
        self.assertEqual(_rank_window(0, False), 3.0)
        window = _rank_window(90, False)
        self.assertIsNotNone(window)
        self.assertGreater(float(window), 3.0)

    def test_busy_pool_window_is_capped(self):
        self.assertEqual(_rank_window(10_000, False), 12)

    def test_within_uses_the_window(self):
        self.assertTrue(_within(5, 0, 3.0) is False)
        self.assertTrue(_within(2, 0, 3.0))
        self.assertTrue(_within(90, 0, None), "thin pool ignores the gap")
        self.assertFalse(_within(90, 0, _rank_window(0, False)))


class ConfigTest(unittest.TestCase):
    def test_group_bounds_are_ints_and_sane(self):
        self.assertIsInstance(config.max_players_per_match, int)
        self.assertIsInstance(config.min_players_per_match, int)
        self.assertGreaterEqual(config.min_players_per_match, 2)
        self.assertLessEqual(config.min_players_per_match, config.max_players_per_match)
        self.assertEqual(config.max_players_per_match, 6,
                         "a match holds up to six runners by default")

    def test_fill_wait_is_not_negative(self):
        self.assertGreaterEqual(config.fill_wait_seconds, 0)


class WaitedSecondsTest(unittest.TestCase):
    def test_garbage_scores_do_not_explode(self):
        self.assertEqual(_waited_seconds(None), 0)
        self.assertEqual(_waited_seconds("nonsense"), 0)


if __name__ == "__main__":
    unittest.main()
