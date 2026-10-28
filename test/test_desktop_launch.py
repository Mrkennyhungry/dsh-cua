import os
import unittest
from unittest.mock import patch

from dsh_desktop import desktop_tools as desktop


class LaunchRegressionTests(unittest.TestCase):
    def setUp(self):
        self.saved = desktop._STATE._launched_windows
        desktop._STATE._launched_windows = []
        self.calc = os.path.join(os.environ['SystemRoot'], 'System32', 'calc.exe')

    def tearDown(self):
        desktop._STATE._launched_windows = self.saved

    def test_packaged_child_is_tracked_as_its_outer_frame(self):
        frame = {'hwnd': 100, 'window_key': '100:1:500000', 'pid': 1, 'exe': 'ApplicationFrameHost.exe', 'title': 'Calculator'}
        child = {'hwnd': 101, 'window_key': '101:2:1000200', 'pid': 2, 'exe': 'CalculatorApp.exe', 'title': ''}
        before = {1: ('applicationframehost.exe', 500.0)}
        after = {**before, 2: ('calculatorapp.exe', 1000.2)}
        with patch.object(desktop.os.path, 'isfile', return_value=True), \
             patch.object(desktop.os, 'startfile') as shell, \
             patch.object(desktop, '_list_visible_windows', side_effect=[[], [child, frame]]), \
             patch.object(desktop, '_launch_process_snapshot', side_effect=[before, after]), \
             patch.object(desktop, '_window_process_ids', return_value={2}), \
             patch.object(desktop, '_current_window_key', return_value=frame['window_key']), \
             patch.object(desktop.win32gui, 'GetAncestor', return_value=100), \
             patch.object(desktop.win32gui, 'GetClassName', return_value='ApplicationFrameWindow'), \
             patch.object(desktop.time, 'time', return_value=1000.0):
            result = desktop.desktop_launch_app.__wrapped__(self.calc, scoped=True, scope_owner='owner')
        self.assertTrue(result['success'])
        self.assertEqual(result['tracked_windows'][0]['hwnd'], 100)
        self.assertEqual(result['tracked_pid'], 2)
        self.assertEqual(desktop._STATE._launched_windows[0]['scope_owner'], 'owner')
        shell.assert_called_once()

    def test_packaged_shortcut_uses_shell_and_preserves_arguments(self):
        from types import SimpleNamespace
        import win32com.client
        shortcut_path = os.path.join(os.environ['SystemRoot'], 'System32', 'calculator.lnk')
        frame = {'hwnd': 100, 'window_key': '100:1:500000', 'pid': 1, 'exe': 'ApplicationFrameHost.exe', 'title': 'Calculator'}
        before = {1: ('applicationframehost.exe', 500.0)}
        after = {**before, 2: ('calculatorapp.exe', 1000.2)}
        shortcut = SimpleNamespace(TargetPath=self.calc, Arguments='--example')
        shell_object = SimpleNamespace(CreateShortCut=lambda _: shortcut)
        with patch.object(desktop.os.path, 'isfile', return_value=True), \
             patch.object(desktop, '_LAUNCH_ROOTS', (os.path.dirname(shortcut_path),)), \
             patch.object(desktop.os, 'startfile') as launch, \
             patch.object(win32com.client, 'Dispatch', return_value=shell_object), \
             patch.object(desktop, '_list_visible_windows', side_effect=[[], [frame]]), \
             patch.object(desktop, '_launch_process_snapshot', side_effect=[before, after]), \
             patch.object(desktop, '_window_process_ids', return_value={2}), \
             patch.object(desktop, '_current_window_key', return_value=frame['window_key']), \
             patch.object(desktop.win32gui, 'GetAncestor', return_value=100), \
             patch.object(desktop.win32gui, 'GetClassName', return_value='ApplicationFrameWindow'), \
             patch.object(desktop.time, 'time', return_value=1000.0):
            result = desktop.desktop_launch_app.__wrapped__(shortcut_path)
        self.assertTrue(result['success'])
        launch.assert_called_once_with(self.calc, 'open', '--example')

    def test_unrelated_new_window_does_not_confirm_launch(self):
        other = {'hwnd': 101, 'window_key': '101:5:999000', 'pid': 5, 'exe': 'other.exe', 'title': 'Other'}
        ticks = iter([0, 0, 0, 20, 30])
        with patch.object(desktop.os.path, 'isfile', return_value=True), \
             patch.object(desktop.os, 'startfile'), \
             patch.object(desktop, '_list_visible_windows', side_effect=[[], [other]]), \
             patch.object(desktop, '_launch_process_snapshot', side_effect=[{}, {5: ('other.exe', 1000.2)}]), \
             patch.object(desktop.time, 'time', return_value=1000.0), \
             patch.object(desktop.time, 'monotonic', side_effect=lambda: next(ticks)), \
             patch.object(desktop.time, 'sleep'):
            result = desktop.desktop_launch_app.__wrapped__(self.calc)
        self.assertFalse(result['success'])
        self.assertTrue(result['launch_uncertain'])
        self.assertIn('LAUNCH_UNCONFIRMED', result['error'])
        self.assertEqual(desktop._STATE._launched_windows, [])

    def test_cmd_uses_explicit_console_host(self):
        import subprocess
        from types import SimpleNamespace
        cmd = os.path.join(os.environ['SystemRoot'], 'System32', 'cmd.exe')
        proc = SimpleNamespace(pid=9, wait=lambda timeout: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd, timeout)))
        with patch.object(desktop, '_is_console_exe', return_value=True), \
             patch('subprocess.Popen', return_value=proc) as spawn:
            actual, early = desktop._popen_launch(cmd)
        self.assertIs(actual, proc)
        self.assertIsNone(early)
        argv = spawn.call_args.args[0]
        self.assertEqual(os.path.basename(argv[0]).lower(), 'conhost.exe')
        self.assertEqual(argv[1], cmd)

    def test_existing_shared_process_does_not_prove_window_ownership(self):
        window = {'hwnd': 101, 'window_key': '101:2:500000', 'pid': 2, 'exe': 'CalculatorApp.exe', 'title': 'Calculator'}
        old_process = {2: ('calculatorapp.exe', 500.0)}
        ticks = iter([0, 0, 0, 20, 30])
        with patch.object(desktop.os.path, 'isfile', return_value=True), \
             patch.object(desktop.os, 'startfile'), \
             patch.object(desktop, '_list_visible_windows', side_effect=[[], [window]]), \
             patch.object(desktop, '_launch_process_snapshot', side_effect=[old_process, old_process]), \
             patch.object(desktop.time, 'time', return_value=1000.0), \
             patch.object(desktop.time, 'monotonic', side_effect=lambda: next(ticks)), \
             patch.object(desktop.time, 'sleep'):
            result = desktop.desktop_launch_app.__wrapped__(self.calc)
        self.assertFalse(result['success'])
        self.assertTrue(result['launch_uncertain'])
        self.assertEqual(desktop._STATE._launched_windows, [])

    def test_live_process_without_window_is_unconfirmed(self):
        from types import SimpleNamespace
        cmd = os.path.join(os.environ['SystemRoot'], 'System32', 'cmd.exe')
        ticks = iter([0, 0, 0, 20, 30])
        with patch.object(desktop.os.path, 'isfile', return_value=True), \
             patch.object(desktop, '_popen_launch', return_value=(SimpleNamespace(pid=9), None)) as launch, \
             patch.object(desktop, '_list_visible_windows', side_effect=[[], []]), \
             patch.object(desktop, '_launch_process_snapshot', side_effect=[{}, {9: ('cmd.exe', 1000.2)}]), \
             patch.object(desktop.psutil, 'Process', return_value=SimpleNamespace(children=lambda **kw: [])), \
             patch.object(desktop.time, 'time', return_value=1000.0), \
             patch.object(desktop.time, 'monotonic', side_effect=lambda: next(ticks)), \
             patch.object(desktop.time, 'sleep'):
            result = desktop.desktop_launch_app.__wrapped__(cmd)
        self.assertFalse(result['success'])
        self.assertTrue(result['launch_uncertain'])
        self.assertEqual(desktop._STATE._launched_windows, [])
        launch.assert_called_once()

    def test_cleanup_reports_unavailable_window_identity(self):
        desktop._STATE._launched_windows = [{'hwnd': 100, 'window_key': '100:1:500000', 'pid': 1, 'scope_owner': 'owner', 't': 1000.0}]
        with patch.object(desktop.win32gui, 'IsWindow', return_value=True), \
             patch.object(desktop.win32gui, 'PostMessage') as close, \
             patch.object(desktop, '_current_window_key', return_value=''):
            result = desktop.cleanup_session_footprint(close_browser=False, scope_owner='owner')
        close.assert_not_called()
        self.assertFalse(result['success'])
        self.assertEqual(len(desktop._STATE._launched_windows), 1)

    def test_unsaved_prompt_prevents_forced_close_and_reports_failure(self):
        desktop._STATE._launched_windows = [{'hwnd': 100, 'window_key': '100:1:500000', 'pid': 1, 'scope_owner': 'owner', 't': 1000.0}]
        with patch.object(desktop.win32gui, 'IsWindow', return_value=True), \
             patch.object(desktop.win32gui, 'PostMessage') as close, \
             patch.object(desktop, '_current_window_key', return_value='100:1:500000'), \
             patch.object(desktop.time, 'monotonic', side_effect=[0, 5]), \
             patch.object(desktop.time, 'sleep'), \
             patch.object(desktop._STATE, '_launched_apps', {}), \
             patch.object(desktop.psutil, 'Process') as process:
            result = desktop.cleanup_session_footprint(close_browser=False, scope_owner='owner', timeout_per_app=1)
        self.assertFalse(result['success'])
        self.assertEqual(len(result['failed_apps']), 1)
        self.assertEqual(len(desktop._STATE._launched_windows), 1)
        close.assert_called_once()
        process.assert_not_called()

    def test_legacy_process_record_is_not_force_terminated(self):
        records = {77: {'name': 'editor', 'scope_owner': 'owner', 't': 1000.0}}
        with patch.object(desktop._STATE, '_launched_apps', records), \
             patch.object(desktop.psutil, 'pid_exists', return_value=True), \
             patch.object(desktop.psutil, 'Process') as process:
            result = desktop.cleanup_session_footprint(close_browser=False, scope_owner='owner')
            self.assertIn(77, desktop._STATE._launched_apps)
        process.assert_not_called()
        self.assertFalse(result['success'])
        self.assertIn('process-only', result['failed_apps'][0])

    def test_cleanup_never_closes_reused_handle(self):
        desktop._STATE._launched_windows = [{'hwnd': 100, 'window_key': '100:1:500000', 'pid': 1, 'scope_owner': 'owner', 't': 1000.0}]
        with patch.object(desktop.win32gui, 'IsWindow', return_value=True), \
             patch.object(desktop.win32gui, 'PostMessage') as close, \
             patch.object(desktop, '_current_window_key', return_value='100:7:700000'):
            result = desktop.cleanup_session_footprint(close_browser=False, scope_owner='owner')
        close.assert_not_called()
        self.assertTrue(result['success'])
        self.assertEqual(desktop._STATE._launched_windows, [])


if __name__ == '__main__':
    unittest.main()
