"""What a click reports about where it landed.

Before this, a click in empty space returned success: the only feedback was "the
screen did not change", which is indistinguishable from a slow app and invites the
caller to nudge the number and retry. These tests pin the correction — name the hit,
or name the nearest element and its coordinates.

They also pin a bug that was live in the reference implementation: the nearest-element
loop compared a tuple slot that was not the distance, so every miss reported the
element closest to the top of the screen instead of the closest to the click.
"""

import time
import unittest

from dsh_desktop import desktop_tools


def remember(window_id, boxes):
    desktop_tools._LAST_OBS[window_id] = {"boxes": boxes, "ts": time.time()}


class ClickHitTests(unittest.TestCase):
    def setUp(self):
        desktop_tools._LAST_OBS.clear()

    def tearDown(self):
        desktop_tools._LAST_OBS.clear()

    def test_no_observation_means_no_claim(self):
        self.assertIsNone(desktop_tools._click_hit_note("win-x", 500, 500))

    def test_point_inside_a_box_names_the_element(self):
        remember("win-x", [(0.0, 0.0, 100.0, 100.0, "保存")])
        self.assertEqual(desktop_tools._click_hit_note("win-x", 50, 50), {"hit": "保存"})

    def test_point_in_empty_space_reports_the_nearest_element(self):
        remember("win-x", [
            (0.0, 0.0, 20.0, 20.0, "顶部"),      # centre (10, 10)
            (400.0, 400.0, 440.0, 440.0, "目标"),  # centre (420, 420), ~35 away from (500,500)
        ])
        note = desktop_tools._click_hit_note("win-x", 500, 500)
        self.assertEqual(note["warning"], "CLICK_HIT_EMPTY")
        self.assertIsNone(note["hit"])
        self.assertEqual(note["nearest"]["text"], "目标")
        self.assertEqual((note["nearest"]["click_x"], note["nearest"]["click_y"]), (420, 420))
        self.assertLess(note["nearest"]["distance"], 120)

    def test_nearest_is_by_distance_not_by_screen_order(self):
        # The bug this pins: comparing the wrong tuple slot reported "顶部" here.
        remember("win-x", [
            (0.0, 0.0, 20.0, 20.0, "顶部"),
            (900.0, 500.0, 920.0, 520.0, "右侧"),
        ])
        note = desktop_tools._click_hit_note("win-x", 880, 510)
        self.assertEqual(note["nearest"]["text"], "右侧")

    def test_stale_observation_is_ignored(self):
        desktop_tools._LAST_OBS["win-x"] = {
            "boxes": [(0.0, 0.0, 100.0, 100.0, "旧")],
            "ts": time.time() - desktop_tools._OBS_TTL_SECONDS - 5,
        }
        self.assertIsNone(desktop_tools._click_hit_note("win-x", 50, 50))

    def test_remembering_a_desktop_scope_observation_clears_the_window(self):
        remember("win-x", [(0.0, 0.0, 100.0, 100.0, "旧")])
        desktop_tools._remember_observation("win-x", [], "desktop")
        self.assertNotIn("win-x", desktop_tools._LAST_OBS)

    def test_timeless_label_falls_back_when_the_box_has_no_text(self):
        remember("win-x", [(0.0, 0.0, 100.0, 100.0, "")])
        self.assertEqual(desktop_tools._click_hit_note("win-x", 50, 50),
                         {"hit": "(无文本元素)"})

    def test_cache_is_bounded(self):
        for index in range(desktop_tools._OBS_MAX_WINDOWS + 5):
            desktop_tools._remember_observation(f"win-{index}", [], "window")
        self.assertLessEqual(len(desktop_tools._LAST_OBS), desktop_tools._OBS_MAX_WINDOWS)


class CoordinateUnitTests(unittest.TestCase):
    def test_window_scope_is_normalized(self):
        contract = desktop_tools._coordinate_contract("window", 1000, 800)
        self.assertEqual(contract["click_unit"], "normalized_0_1000")
        self.assertEqual(contract["click_space"], "window")

    def test_desktop_scope_is_screen_pixels(self):
        contract = desktop_tools._coordinate_contract("desktop", 3840, 2160)
        self.assertEqual(contract["click_unit"], "screen_pixels")
        self.assertEqual(contract["click_space"], "screen")
        self.assertIn("desktop_click_screen", contract["rule"])


if __name__ == "__main__":
    unittest.main()
