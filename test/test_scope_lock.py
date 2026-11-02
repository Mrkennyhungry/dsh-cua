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

    def test_other_windows_are_refused_while_locked(self):
        info = {"window_id": "win-a", "title": "记事本", "exe": "notepad.exe", "hwnd": 10, "pid": 99}
        with mock.patch.object(desktop_tools, "bound_window_info", return_value=info):
            self.assertTrue(desktop_tools.lock_window("win-a")["success"])
            self.assertIsNone(desktop_tools.scope_block("win-a"))
            blocked = desktop_tools.scope_block("win-b")
        self.assertIsNotNone(blocked)
        self.assertIn("WINDOW_SCOPE_LOCKED", blocked["error"])
        self.assertEqual(blocked["locked_window"]["window_id"], "win-a")

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
