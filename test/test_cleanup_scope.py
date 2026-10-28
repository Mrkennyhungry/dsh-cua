"""Cleanup must only touch windows this session registered and can still prove.

The old implementation swept every visible window whose exe matched a keyword,
so a user's own Notepad could be closed by a task that merely launched one.
These tests pin the registered-window contract.
"""

import unittest
from unittest import mock

from dsh_desktop import desktop_tools


class CleanupScopeTests(unittest.TestCase):
    def setUp(self):
        self._saved = getattr(desktop_tools._STATE, "_launched_windows", [])
        desktop_tools._STATE._launched_windows = []

    def tearDown(self):
        desktop_tools._STATE._launched_windows = self._saved

    def test_without_registration_nothing_is_even_enumerated(self):
        with mock.patch("win32gui.EnumWindows") as enum, \
                mock.patch("win32gui.PostMessage") as post:
            result = desktop_tools.desktop_cleanup_windows_by_exe(exe_keyword="notepad")
        enum.assert_not_called()
        post.assert_not_called()
        self.assertTrue(result["success"])
        self.assertEqual(result["closed"], 0)

    def test_registered_window_with_changed_identity_is_kept(self):
        desktop_tools._STATE._launched_windows = [{
            "hwnd": 4242, "exe": "notepad.exe", "title": "old",
            "window_key": "4242:1:100", "scope_owner": "s1", "t": 1.0,
        }]
        with mock.patch("win32gui.IsWindow", return_value=True), \
                mock.patch.object(desktop_tools, "_current_window_key", return_value="4242:9:999"), \
                mock.patch("win32gui.PostMessage") as post:
            result = desktop_tools.desktop_cleanup_windows_by_exe()
        post.assert_not_called()
        self.assertFalse(result["success"])
        self.assertEqual(result["closed"], 0)
        self.assertTrue(any("复用" in item for item in result["failed"]))

    def test_save_dialog_keeps_window_and_reports_incomplete(self):
        desktop_tools._STATE._launched_windows = [{
            "hwnd": 77, "exe": "notepad.exe", "title": "dirty.txt",
            "window_key": "77:1:100", "scope_owner": "s1", "t": 1.0,
        }]
        with mock.patch("win32gui.IsWindow", return_value=True), \
                mock.patch.object(desktop_tools, "_current_window_key", return_value="77:1:100"), \
                mock.patch("win32gui.PostMessage") as post:
            result = desktop_tools.desktop_cleanup_windows_by_exe()
        post.assert_called_once()
        self.assertFalse(result["success"])
        self.assertEqual(result["closed"], 0)
        self.assertEqual(result["remaining"], 1)
        self.assertTrue(any("未关闭" in item for item in result["failed"]))

    def test_matching_window_closes(self):
        desktop_tools._STATE._launched_windows = [{
            "hwnd": 88, "exe": "calc.exe", "title": "计算器",
            "window_key": "88:1:100", "scope_owner": "s1", "t": 1.0,
        }]
        # IsWindow is consulted three times: pre-filter, wait loop, final check.
        with mock.patch("win32gui.IsWindow", side_effect=[True, False, False]), \
                mock.patch.object(desktop_tools, "_current_window_key", return_value="88:1:100"), \
                mock.patch("win32gui.PostMessage") as post:
            result = desktop_tools.desktop_cleanup_windows_by_exe()
        post.assert_called_once()
        self.assertTrue(result["success"])
        self.assertEqual(result["closed"], 1)

    def test_scope_owner_filters_registered_windows(self):
        desktop_tools._STATE._launched_windows = [{
            "hwnd": 99, "exe": "calc.exe", "title": "other task",
            "window_key": "99:1:100", "scope_owner": "another-session", "t": 1.0,
        }]
        with mock.patch("win32gui.IsWindow", return_value=True), \
                mock.patch("win32gui.PostMessage") as post:
            result = desktop_tools.desktop_cleanup_windows_by_exe(scope_owner="mine")
        post.assert_not_called()
        self.assertEqual(result["matched"], 0)


if __name__ == "__main__":
    unittest.main()
