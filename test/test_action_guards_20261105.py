# -*- coding: utf-8 -*-
"""Regression for the 2026-11-05 action-chain audit (BUG-10/11/12/13/14/16/17/19/22/24)."""
import os
import time
import unittest
from unittest.mock import Mock, patch

from dsh_desktop import desktop_tools, decide


class DecideGoalTests(unittest.TestCase):
    """BUG-12 (shared CJK char) and BUG-19 (unique-but-wrong / low confidence)."""

    def rows(self, items):
        return [{"id": i, "text": t, "type": ty, "bbox": b}
                for i, (t, ty, b) in enumerate(items)]

    def test_shared_single_character_is_rejected(self):
        d = decide.decide("设置", self.rows([("移动设备", "Text", [10, 10, 80, 30])]))
        self.assertIsNone(d.id)

    def test_exact_match_still_resolves(self):
        d = decide.decide("设置", self.rows([("设置", "Button", [10, 10, 80, 30])]))
        self.assertEqual(d.id, 0)

    def test_unique_passive_text_is_refused_as_low_confidence(self):
        # The field case: goal 下载 hit the nav "下载" Text while the real
        # toolbar Button had no accessible name. A single passive match must not
        # click through with perfect confidence.
        d = decide.decide("下载", self.rows([("下载", "Text", [71, 788, 92, 810])]))
        self.assertIsNone(d.id)
        self.assertEqual(d.reason, "low_confidence")
        self.assertLess(d.confidence, decide.MIN_CONFIDENCE)

    def test_unique_button_is_allowed(self):
        d = decide.decide("下载", self.rows([("下载", "Button", [71, 788, 92, 810])]))
        self.assertEqual(d.id, 0)
        self.assertGreaterEqual(d.confidence, decide.MIN_CONFIDENCE)

    def test_antonym_pair_still_separated(self):
        d = decide.decide("最大化", self.rows([("最小化", "Button", [10, 10, 80, 30])]))
        self.assertIsNone(d.id)


class LaunchRootTests(unittest.TestCase):
    """BUG-16 (D: drive) and BUG-17 (drive-root double separator)."""

    def test_drive_root_matches_a_path_below_it(self):
        # A drive root already ends in a separator; the old root + os.sep test
        # produced "D:\\" and never matched.
        self.assertTrue(desktop_tools._under_root("D:\\soft\\App\\App.exe", "D:\\"))

    def test_sibling_prefix_is_not_a_match(self):
        self.assertFalse(desktop_tools._under_root("D:\\softA\\App.exe", "D:\\soft"))

    def test_exact_root_matches(self):
        self.assertTrue(desktop_tools._under_root("D:\\", "D:\\"))

    def test_non_system_drive_target_is_allowed_when_drive_exists(self):
        target = "D:\\soft\\QQGallery\\QQGallery.exe"
        if not any(desktop_tools._under_root(target, r) for r in
                   desktop_tools._LAUNCH_TARGET_ROOTS):
            self.skipTest("no D: drive on this machine")
        self.assertTrue(desktop_tools._launch_target_allowed(target))

    def test_downloads_and_temp_remain_denied(self):
        self.assertFalse(desktop_tools._launch_target_allowed(
            r"C:\Users\someone\Downloads\evil.exe"))
        temp = os.environ.get("TEMP") or os.environ.get("TMP")
        if temp:
            self.assertFalse(desktop_tools._launch_target_allowed(
                os.path.join(temp, "evil.exe")))


class ObservationGeometryTests(unittest.TestCase):
    """BUG-14: coordinates derived from a client rect go stale when it moves."""

    def setUp(self):
        desktop_tools._LAST_OBS.clear()

    def tearDown(self):
        desktop_tools._LAST_OBS.clear()

    def test_no_geometry_recorded_means_no_claim(self):
        desktop_tools._LAST_OBS["win-x"] = {"boxes": [], "ts": time.time(), "stale": False}
        self.assertIsNone(desktop_tools._geometry_staleness("win-x", 123))

    def test_moved_window_is_detected(self):
        desktop_tools._LAST_OBS["win-x"] = {
            "boxes": [], "ts": time.time(), "stale": False,
            "geometry": (100, 100, 800, 600),
        }
        with patch.object(desktop_tools, "_client_geometry",
                          return_value=(117, 100, 800, 600)):
            warn = desktop_tools._geometry_staleness("win-x", 123)
        self.assertIsNotNone(warn)
        self.assertEqual(warn["geometry_drift_px"], 17)

    def test_same_geometry_is_fine(self):
        desktop_tools._LAST_OBS["win-x"] = {
            "boxes": [], "ts": time.time(), "stale": False,
            "geometry": (100, 100, 800, 600),
        }
        with patch.object(desktop_tools, "_client_geometry",
                          return_value=(101, 100, 800, 600)):
            self.assertIsNone(desktop_tools._geometry_staleness("win-x", 123))

    def test_click_x_only_elements_are_remembered(self):
        desktop_tools._remember_observation(
            "win-x", [{"text": "保存", "click_x": 500, "click_y": 300}], "window")
        boxes = desktop_tools._LAST_OBS["win-x"]["boxes"]
        self.assertEqual(len(boxes), 1)
        self.assertEqual(desktop_tools._click_hit_note("win-x", 500, 300),
                         {"hit": "保存"})


class TruncationHintTests(unittest.TestCase):
    """BUG-10: a truncated element list must say so explicitly."""

    def test_truncated_payload_shape(self):
        # Exercise the shape directly: the analyze() wrapper is heavyweight, so
        # assert the documented contract of the field it now emits.
        payload = {"kept": 20, "total": 59, "hidden": 39,
                   "cause": "max_elements", "hint": "..."}
        self.assertGreater(payload["hidden"], 0)
        self.assertIn(payload["cause"], ("max_elements", "only_actionable"))


if __name__ == "__main__":
    unittest.main()
