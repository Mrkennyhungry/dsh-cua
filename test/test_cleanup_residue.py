"""Residue = windows we can prove we created, not everything that appeared.

A window the user opens while the task runs is not residue; reporting it as
such made cleanup report failure even though every task window was closed.
"""

import unittest
from unittest import mock

from dsh_desktop import desktop_tools


def _win(hwnd, pid, created, title="w"):
    return {"hwnd": hwnd, "pid": pid, "process_created": created,
            "window_key": f"{hwnd}:{pid}:{int(created * 1000)}", "title": title}


class ResidueOwnershipTests(unittest.TestCase):
    def setUp(self):
        self._apps = getattr(desktop_tools._STATE, "_launched_apps", {})
        self._wins = getattr(desktop_tools._STATE, "_windows", {})
        desktop_tools._STATE._launched_apps = {}
        desktop_tools._STATE._windows = {}
        self.addCleanup(setattr, desktop_tools._STATE, "_launched_apps", self._apps)
        self.addCleanup(setattr, desktop_tools._STATE, "_windows", self._wins)

    def test_window_of_launched_process_is_residue(self):
        desktop_tools._STATE._launched_apps[4242] = {
            "exe": "calc.exe", "name": "计算器", "t": 0.0, "scope_owner": "s",
            "create_time": 1000.0,
        }
        with mock.patch.object(desktop_tools, "_process_create_time", return_value=1000.0):
            residue = desktop_tools.task_residue_windows([_win(1, 4242, 1000.0)])
        self.assertEqual(len(residue), 1)

    def test_window_of_other_process_is_not_residue(self):
        desktop_tools._STATE._launched_apps[4242] = {
            "exe": "calc.exe", "name": "计算器", "t": 0.0, "scope_owner": "s",
            "create_time": 1000.0,
        }
        # The user opened ChatGPT while the task was running.
        with mock.patch.object(desktop_tools, "_process_create_time", return_value=1000.0):
            residue = desktop_tools.task_residue_windows([
                _win(1, 4242, 1000.0),
                _win(2, 9999, 2000.0, title="ChatGPT"),
            ])
        self.assertEqual([w["title"] for w in residue], ["w"])

    def test_bound_window_counts_as_ours(self):
        desktop_tools._STATE._windows["w1"] = {"hwnd": 5, "pid": 7777}
        residue = desktop_tools.task_residue_windows([_win(5, 7777, 3000.0)])
        self.assertEqual(len(residue), 1)

    def test_ownership_snapshot_is_independent_of_cleanup_clearing_records(self):
        desktop_tools._STATE._launched_apps[11] = {
            "exe": "x.exe", "name": "x", "t": 0.0, "scope_owner": "s",
            "create_time": 500.0,
        }
        desktop_tools._STATE._windows["w"] = {"hwnd": 3, "pid": 22}
        snap = desktop_tools.ownership_snapshot()
        desktop_tools._STATE._launched_apps = {}
        desktop_tools._STATE._windows = {}
        residue = desktop_tools.task_residue_windows(
            [_win(3, 22, 3000.0), _win(4, 11, 500.0)], snap)
        self.assertEqual({w["hwnd"] for w in residue}, {3, 4})

    def test_same_pid_different_process_is_not_ours(self):
        # The pid was recycled: the window belongs to a different process now.
        desktop_tools._STATE._launched_apps[4242] = {
            "exe": "calc.exe", "name": "计算器", "t": 0.0, "scope_owner": "s",
            "create_time": 1000.0,
        }
        residue = desktop_tools.task_residue_windows([_win(1, 4242, 9999.0)])
        self.assertEqual(residue, [])


if __name__ == "__main__":
    unittest.main()
