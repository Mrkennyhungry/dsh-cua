"""The pixel verdict must accept real input and reject caret-level noise."""

import unittest
from unittest import mock

from dsh_desktop import dsh_bridge


class PixelVerdictTests(unittest.TestCase):
    def test_text_insert_is_confirmed(self):
        # Measured live on 2026-11-02: typing one line into a 460x240 window.
        self.assertTrue(dsh_bridge._pixel_change_confirms({"cells": 20, "mean": 0.48}))

    def test_broad_repaint_with_few_cells_is_confirmed(self):
        self.assertTrue(dsh_bridge._pixel_change_confirms({"cells": 3, "mean": 2.5}))

    def test_caret_blink_is_not_confirmed(self):
        self.assertFalse(dsh_bridge._pixel_change_confirms({"cells": 2, "mean": 0.3}))

    def test_empty_delta_is_not_confirmed(self):
        self.assertFalse(dsh_bridge._pixel_change_confirms(None))
        self.assertFalse(dsh_bridge._pixel_change_confirms({}))

    def test_old_loose_threshold_would_have_confirmed_noise(self):
        # 10 cells / 1.2 mean used to be enough; both are now below the bar.
        self.assertFalse(dsh_bridge._pixel_change_confirms({"cells": 10, "mean": 1.2}))


class RelatedWindowTests(unittest.TestCase):
    def test_same_process_counts_as_related(self):
        with mock.patch.object(dsh_bridge.desktop_tools, "bound_window_info",
                               return_value={"hwnd": 100, "pid": 4242}):
            related, unrelated = dsh_bridge._split_related_windows(
                "win-1", [{"hwnd": 200, "pid": 4242}, {"hwnd": 300, "pid": 999}])
        self.assertEqual([w["hwnd"] for w in related], [200])
        self.assertEqual([w["hwnd"] for w in unrelated], [300])

    def test_unknown_target_pid_treats_everything_as_unrelated(self):
        with mock.patch.object(dsh_bridge.desktop_tools, "bound_window_info", return_value={}):
            related, unrelated = dsh_bridge._split_related_windows(
                "win-1", [{"hwnd": 200, "pid": 4242}])
        self.assertEqual(related, [])
        self.assertEqual(len(unrelated), 1)


if __name__ == "__main__":
    unittest.main()
