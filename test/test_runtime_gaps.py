"""Regressions for the six-tool coverage added after the 0.5 release."""
from types import SimpleNamespace
import threading
import unittest
from unittest import mock

from dsh_desktop.actions import ActionDispatcher
from dsh_desktop.browser import BrowserRuntime
from dsh_desktop.conditions import Conditions, validate_condition
from dsh_desktop.runtime import DesktopRuntime
from dsh_desktop.targets import RuntimeFault


class CoverageTests(unittest.TestCase):
    def test_physical_popup_click_requires_target_owner_chain(self):
        target = SimpleNamespace(kind='window', handle=42)
        with mock.patch('win32gui.WindowFromPoint', return_value=90), \
                mock.patch('win32gui.GetAncestor', return_value=90), \
                mock.patch('dsh_desktop.game.window._has_owner_link', return_value=True):
            ActionDispatcher._verify_hit(target, (10, 20))
        with mock.patch('win32gui.WindowFromPoint', return_value=90), \
                mock.patch('win32gui.GetAncestor', return_value=90), \
                mock.patch('dsh_desktop.game.window._has_owner_link', return_value=False):
            with self.assertRaisesRegex(RuntimeFault, 'covered'):
                ActionDispatcher._verify_hit(target, (10, 20))

    def test_rebind_requires_unique_title_and_process_instance(self):
        runtime = mock.Mock()
        old = SimpleNamespace(kind='window', target_id='old', metadata={
            'title': 'App', 'window_key': '10:42:1234'})
        matching = {'kind': 'window', 'target_id': 'new', 'title': 'App', 'window_key': '11:42:1234'}
        runtime.target_directory.return_value = [matching]
        replacement = DesktopRuntime.rebind_window(runtime, 'a', old)
        self.assertIs(replacement, runtime.resolve_target.return_value)
        runtime.resolve_target.assert_called_once_with('a', 'new')
        runtime.target_directory.return_value = [matching, {**matching, 'target_id': 'ambiguous'}]
        self.assertIsNone(DesktopRuntime.rebind_window(runtime, 'a', old))
        runtime.target_directory.return_value = [{**matching, 'window_key': '11:42:9999'}]
        self.assertIsNone(DesktopRuntime.rebind_window(runtime, 'a', old))

    def test_window_cleanup_releases_only_its_binding_and_restores_effects(self):
        runtime = mock.Mock()
        runtime._window_effects = {}
        runtime._release_window_effect.side_effect = lambda *args: DesktopRuntime._release_window_effect(runtime, *args)
        target = SimpleNamespace(target_id='t', kind='window', handle=42, closed=False)
        runtime.resolve_target.return_value = target
        with mock.patch('dsh_desktop.runtime.win32gui.GetWindowLong', return_value=0), \
                mock.patch('dsh_desktop.desktop_tools.set_topmost') as topmost:
            DesktopRuntime.window_action(runtime, 'a', target, {'op': 'topmost'}, 'operation')
            self.assertEqual(topmost.call_args.args, (42, True))
            with self.assertRaises(RuntimeFault):
                DesktopRuntime.window_action(runtime, 'b', target, {'op': 'topmost'}, 'other')
            DesktopRuntime.window_action(runtime, 'a', target, {'op': 'cleanup'}, 'operation')
            self.assertEqual(topmost.call_args.args, (42, False))
        self.assertTrue(target.closed)
        runtime.operations.stop_target.assert_called_once_with('a', 't', excluding='operation')
        runtime.observations.release_target.assert_called_once_with('a', 't')
        runtime.frames.release_target.assert_called_once_with('a', 't')
        self.assertEqual(runtime._window_effects, {})

    def test_window_and_query_arguments_reject_ambiguous_combinations(self):
        base = {'target_id': 't', 'observation_id': 'o'}
        for step in ({'action': 'window', 'op': 'topmost', 'on': True},
                     {'action': 'window', 'op': 'unbind'},
                     {'action': 'dom_query', 'selector': 'button'},
                     {'action': 'extract', 'selector': 'a', 'limit': 100}):
            ActionDispatcher.validate({**base, 'steps': [step]})
        for steps in ([{'action': 'window', 'op': 'cleanup'}, {'action': 'close'}],
                      [{'action': 'window', 'op': 'activate', 'on': True}],
                      [{'action': 'extract', 'selector': ''}],
                      [{'action': 'dom_query', 'selector': 'body', 'limit': 101}]):
            with self.assertRaises(ValueError):
                ActionDispatcher.validate({**base, 'steps': steps})

    def test_css_query_uses_target_frame_and_bounds_output(self):
        runtime = SimpleNamespace(epoch='test')
        browser = BrowserRuntime(runtime)
        frame = mock.Mock()
        browser.resolve = mock.Mock(return_value=SimpleNamespace(handle=(None, frame)))
        frame.evaluate.return_value = {'nodes': [{'text': 'example'}], 'total': 1}
        result = browser.query('a', 't', {'action': 'extract', 'selector': 'a'})
        browser.resolve.assert_called_once_with('a', 't')
        self.assertEqual(result['nodes'][0]['text'], 'example')
        self.assertEqual(frame.evaluate.call_args.args[1], {'selector': 'a', 'limit': 50, 'extract': True})
        frame.evaluate.side_effect = Exception('invalid selector')
        with self.assertRaisesRegex(RuntimeFault, 'CSS selector'):
            browser.query('a', 't', {'action': 'dom_query', 'selector': '['})

    def test_settle_requires_a_continuous_quiet_interval(self):
        conditions = Conditions(mock.Mock())
        signatures = iter(['a', 'b', 'b', 'b', 'b'])
        conditions.sample = mock.Mock(side_effect=lambda *args: (True, {
            'settle_evidence': {'signature': next(signatures)}}))
        context = mock.Mock(owner='a', cancelled=threading.Event())
        with mock.patch('dsh_desktop.conditions.time.monotonic', side_effect=[
                0, 0, 0, 0, 0, 0.05, 0.05, 0.05, 0.05, 0.15, 0.15]):
            # A changing signature resets the timer before a later stable sample.
            try:
                result = conditions.wait(context, 't', 'o', {'kind': 'settle', 'quiet_ms': 100}, 1000)
            except StopIteration:
                self.fail('settle failed to return after the quiet interval')
        self.assertEqual(result['expectation']['status'], 'met')
        self.assertGreaterEqual(result['settle_evidence']['quiet_ms'], 100)
        validate_condition({'kind': 'settle', 'quiet_ms': 100})
        with self.assertRaises(ValueError):
            validate_condition({'kind': 'settle', 'quiet_ms': 0})
