"""Kernel-wide window lock and the emergency stop flag."""

import unittest
from unittest import mock

from dsh_desktop import desktop_tools, input_gate


class ScopeLockTests(unittest.TestCase):
    def setUp(self):
        desktop_tools.unlock_window()
        input_gate.clear_stop()

    def tearDown(self):
        desktop_tools.unlock_window()
        input_gate.clear_stop()

    def test_nothing_is_locked_by_default(self):
        self.assertIsNone(desktop_tools.locked_window_id())
        self.assertIsNone(desktop_tools.scope_block("win-1"))

    def test_lock_requires_a_bound_window(self):
        with mock.patch.object(desktop_tools, "bound_window_info", return_value=None):
            result = desktop_tools.lock_window("win-missing")
        self.assertFalse(result["success"])
        self.assertIsNone(desktop_tools.locked_window_id())

    def test_other_applications_are_refused_while_locked(self):
        infos = {
            "win-a": {"title": "记事本", "exe": "notepad.exe", "hwnd": 10, "pid": 99},
            "win-b": {"title": "浏览器", "exe": "msedge.exe", "hwnd": 20, "pid": 100},
        }
        with mock.patch.object(desktop_tools, "bound_window_info",
                               side_effect=lambda window_id: infos.get(window_id, {})):
            self.assertTrue(desktop_tools.lock_window("win-a")["success"])
            self.assertIsNone(desktop_tools.scope_block("win-a"))
            blocked = desktop_tools.scope_block("win-b")
        self.assertIsNotNone(blocked)
        self.assertIn("WINDOW_SCOPE_LOCKED", blocked["error"])
        self.assertEqual(blocked["locked_window"]["window_id"], "win-a")

    def test_new_window_of_same_application_is_allowed(self):
        infos = {
            "win-a": {"title": "文档 1", "exe": "notepad.exe", "hwnd": 10, "pid": 99},
            "win-new": {"title": "文档 2", "exe": "Notepad.exe", "hwnd": 30, "pid": 120},
        }
        with mock.patch.object(desktop_tools, "bound_window_info",
                               side_effect=lambda window_id: infos.get(window_id, {})):
            desktop_tools.lock_window("win-a")
            self.assertIsNone(desktop_tools.scope_block("win-new"))

    def test_owned_popup_is_allowed_even_without_exe_identity(self):
        infos = {
            "win-a": {"title": "主窗口", "exe": "app.exe", "hwnd": 10, "pid": 99},
            "popup": {"title": "保存", "exe": "", "hwnd": 30, "pid": 120},
        }
        with mock.patch.object(desktop_tools, "bound_window_info",
                               side_effect=lambda window_id: infos.get(window_id, {})), \
                mock.patch("dsh_desktop.game.window._has_owner_link",
                           side_effect=lambda candidate, owner: (candidate, owner) == (30, 10)):
            desktop_tools.lock_window("win-a")
            self.assertIsNone(desktop_tools.scope_block("popup"))

    def test_lock_adds_and_unlock_one_preserves_the_other(self):
        infos = {
            "win-a": {"title": "记事本", "exe": "notepad.exe", "hwnd": 10, "pid": 99},
            "win-b": {"title": "浏览器", "exe": "msedge.exe", "hwnd": 20, "pid": 100},
            "win-c": {"title": "终端", "exe": "cmd.exe", "hwnd": 30, "pid": 101},
        }
        with mock.patch.object(desktop_tools, "bound_window_info",
                               side_effect=lambda window_id: infos.get(window_id, {})):
            desktop_tools.lock_window("win-a")
            desktop_tools.lock_window("win-b")
            self.assertEqual(desktop_tools.locked_window_ids(), ["win-a", "win-b"])
            self.assertIsNone(desktop_tools.scope_block("win-b"))
            result = desktop_tools.unlock_window("win-a")
            self.assertEqual(result["unlocked"], "win-a")
            self.assertEqual(desktop_tools.locked_window_ids(), ["win-b"])
            blocked = desktop_tools.scope_block("win-c")
        self.assertIn("WINDOW_SCOPE_LOCKED", blocked["error"])

    def test_absolute_pixel_input_cannot_bypass_scope(self):
        infos = {
            "win-a": {"title": "记事本", "exe": "notepad.exe", "hwnd": 10, "pid": 99},
        }
        with mock.patch.object(desktop_tools, "bound_window_info", return_value=infos["win-a"]), \
                mock.patch.object(desktop_tools, "_window_info",
                                  side_effect=lambda hwnd: infos["win-a"] if hwnd == 10 else {
                                      "title": "浏览器", "exe": "msedge.exe", "hwnd": 20, "pid": 100}), \
                mock.patch("win32gui.WindowFromPoint", side_effect=lambda point: point[0]), \
                mock.patch("win32gui.GetAncestor", side_effect=lambda hwnd, _kind: hwnd):
            desktop_tools.lock_window("win-a")
            self.assertIsNone(desktop_tools.scope_block_point(10, 100))
            blocked = desktop_tools.scope_block_point(20, 100)
        self.assertIn("WINDOW_SCOPE_LOCKED", blocked["error"])

    def test_empty_scope_allows_launch_and_absolute_pixel_input(self):
        self.assertIsNone(desktop_tools.scope_launch_refusal("C:/Windows/System32/notepad.exe"))
        self.assertIsNone(desktop_tools.scope_block_point(20, 100))

    def test_launch_scope_is_user_controlled_and_names_current_scope(self):
        info = {"title": "iOA", "exe": "iOA.exe", "hwnd": 10, "pid": 99}
        with mock.patch.object(desktop_tools, "bound_window_info", return_value=info):
            desktop_tools.lock_window("win-ioa")
        blocked = desktop_tools.scope_launch_refusal("C:/Windows/System32/notepad.exe")
        self.assertIn("WINDOW_SCOPE_LOCKED", blocked["error"])
        self.assertIn("iOA", blocked["error"])
        self.assertIn("右上角吉祥物", blocked["error"])
        self.assertIn("AI 不得自行添加或解除授权", blocked["error"])

    def test_launch_of_the_already_authorized_application_is_allowed(self):
        info = {"title": "记事本", "exe": "notepad.exe", "hwnd": 10, "pid": 99}
        with mock.patch.object(desktop_tools, "bound_window_info", return_value=info):
            desktop_tools.lock_window("win-notepad")
        self.assertIsNone(desktop_tools.scope_launch_refusal("C:/Windows/System32/notepad.exe"))

    def test_unlock_restores_access(self):
        info = {"window_id": "win-a", "title": "t", "exe": "e", "hwnd": 1, "pid": 2}
        with mock.patch.object(desktop_tools, "bound_window_info", return_value=info):
            desktop_tools.lock_window("win-a")
            desktop_tools.unlock_window()
            self.assertIsNone(desktop_tools.scope_block("win-b"))


class EmergencyStopTests(unittest.TestCase):
    def tearDown(self):
        input_gate.clear_stop()

    def test_stop_refuses_input_until_cleared(self):
        with mock.patch.object(input_gate, "session_blocker", return_value=None):
            input_gate.clear_stop()
            self.assertIsNone(input_gate.guard())
            input_gate.emergency_stop()
            blocked = input_gate.guard()
            self.assertIsNotNone(blocked)
            self.assertIn("INPUT_STOPPED", blocked["error"])
            input_gate.clear_stop()
            self.assertIsNone(input_gate.guard())

    def test_stop_state_is_reported(self):
        input_gate.emergency_stop()
        self.assertTrue(input_gate.stop_engaged())
        input_gate.clear_stop()
        self.assertFalse(input_gate.stop_engaged())


if __name__ == "__main__":
    unittest.main()
