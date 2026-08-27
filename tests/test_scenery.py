import unittest

from load_scenic_features import category_for
from scenery import score_signals


class FakeTags(dict):
    pass


class SceneryTests(unittest.TestCase):
    def test_osm_categories_are_explicit(self):
        self.assertEqual(category_for(FakeTags(natural="water")), "water")
        self.assertEqual(category_for(FakeTags(landuse="forest")), "forest")
        self.assertEqual(category_for(FakeTags(tourism="viewpoint")), "viewpoint")
        self.assertIsNone(category_for(FakeTags(waterway="stream")))
        self.assertIsNone(category_for(FakeTags(leisure="park")))
        self.assertIsNone(category_for(FakeTags(shop="supermarket")))

    def test_signal_score_is_additive_and_capped(self):
        self.assertEqual(score_signals({"water", "forest"}), 55)
        self.assertEqual(
            score_signals({"water", "forest", "park", "countryside", "natural", "viewpoint"}),
            100,
        )


if __name__ == "__main__":
    unittest.main()
