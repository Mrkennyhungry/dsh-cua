"""Untitled-window discovery and identity-safe process/owner handoffs."""
from types import SimpleNamespace
from contextlib import nullcontext
import threading
import unittest
from unittest import mock

from dsh_desktop import desktop_tools as desktop
from dsh_desktop.actions import ActionDispatcher
from dsh_desktop.observations import ObservationService
from dsh_desktop.runtime import DesktopRuntime
from dsh_desktop.targets import RuntimeFault


class WindowDiscoveryTests(unittest.TestCase):
    def test_click_returns_new_windows_and_stops_batch_with_dispatch_preserved(self):
        for closed in (True, False):
            with self.subTest(closed=closed):
                runtime = mock.Mock()
                runtime.resolve_target.return_value = SimpleNamespace(kind='window', handle=12, metadata={})
                runtime.observations.get.return_value = SimpleNamespace(metadata={})
                runtime.observations.capture_frame.side_effect = RuntimeFault('TARGET_CLOSED', 'capture unavailable')
                candidates = [{'target_id': 'child', 'hwnd': 13, 'relationship': 'child_process'}]
                runtime.new_window_candidates.side_effect = [[], candidates]
                context = mock.Mock(owner='a', cancelled=threading.Event())
                context.hold.side_effect = lambda lock: nullcontext()
                dispatcher = ActionDispatcher(runtime)
                dispatcher._step = mock.Mock(return_value='physical_click')
                dispatcher._boundary = mock.Mock(side_effect=[('stable',),
                    RuntimeFault('TARGET_CLOSED', 'closed') if closed else ('stable',)])
                dispatcher._readback = mock.Mock(return_value={'closed': closed})
                with mock.patch.object(desktop, 'window_identity_snapshot', return_value={12: {'window_key': '12:42:1000'}}), \
                        mock.patch.object(desktop.win32gui, 'IsWindow', return_value=not closed), \
                        mock.patch('dsh_desktop.game.window.is_foreground_target', return_value=True):
                    result = dispatcher._batch(context, {'target_id': 'old', 'observation_id': 'o', 'steps': [
                        {'action': 'click', 'target': {'ref': 'e'}}, {'action': 'key', 'keys': ['TAB']}]})
                self.assertTrue(result['success'])
                self.assertEqual(result['action']['status'], 'dispatched')
                self.assertEqual(result['new_window_candidates'], candidates)
                self.assertEqual(result['outcome'], 'interrupted')
                dispatcher._step.assert_called_once()
                dispatcher._readback.assert_called_once()

    def test_ordinary_process_metadata_skips_ancestry_scans(self):
        process = mock.Mock()
        process.name.return_value = 'app.exe'
        process.create_time.return_value = 1.0
        with mock.patch.object(desktop.psutil, 'Process', return_value=process):
            details = desktop._process_details(42)
        self.assertEqual(details['process_key'], '42:1000')
        self.assertEqual(details['process_ancestors'], [])
        process.parent.assert_not_called()

    def test_action_snapshot_avoids_title_reads_and_reuses_process_query(self):
        def enumerate_windows(callback, value):
            callback(12, value)
            callback(13, value)
        process = mock.Mock()
        process.create_time.return_value = 1.0
        with mock.patch.object(desktop.win32gui, 'EnumWindows', side_effect=enumerate_windows), \
                mock.patch('dsh_desktop.decorations.is_decoration_window', return_value=False), \
                mock.patch.object(desktop.win32gui, 'IsWindowVisible', return_value=True), \
                mock.patch.object(desktop.win32gui, 'GetWindow', return_value=0), \
                mock.patch.object(desktop.win32process, 'GetWindowThreadProcessId', return_value=(1, 42)), \
                mock.patch.object(desktop.psutil, 'Process', return_value=process) as lookup, \
                mock.patch.object(desktop, '_safe_window_title', side_effect=AssertionError('unexpected title read')):
            rows = desktop.window_identity_snapshot()
        self.assertEqual(rows[12]['window_key'], '12:42:1000')
        self.assertEqual(rows[13]['window_key'], '13:42:1000')
        lookup.assert_called_once_with(42)

    def test_enumeration_includes_untitled_by_default(self):
        def enumerate_windows(callback, value):
            callback(12, value)
        with mock.patch.object(desktop.win32gui, 'EnumWindows', side_effect=enumerate_windows), \
                mock.patch.object(desktop, '_describe_window', return_value={'hwnd': 12}) as describe:
            self.assertEqual(desktop._list_visible_windows(), [{'hwnd': 12}])
            self.assertTrue(describe.call_args.kwargs['include_untitled'])
            desktop._list_visible_windows(include_untitled=False)
            self.assertFalse(describe.call_args.kwargs['include_untitled'])

    def test_directory_filter_applies_to_already_adopted_untitled_windows(self):
        runtime = mock.Mock()
        runtime.browser._context_instance = None
        runtime.browser._connections = []
        runtime.targets.records.return_value = []
        rows = [{'kind': 'desktop'}, {'kind': 'window', 'title': 'Panel', 'window_text': ''},
                {'kind': 'window', 'title': 'App', 'window_text': 'App'}]
        runtime.targets.directory.return_value = rows
        with mock.patch('win32api.EnumDisplayMonitors', return_value=[]), \
                mock.patch('dsh_desktop.runtime.capture.virtual_origin', return_value=(0, 0)), \
                mock.patch.object(desktop, '_list_visible_windows', return_value=[]):
            self.assertEqual(DesktopRuntime.target_directory(runtime, 'a'), rows)
            self.assertEqual(DesktopRuntime.target_directory(runtime, 'a', include_untitled=False), [rows[0], rows[2]])

    def test_untitled_window_provenance_and_explicit_exclusion(self):
        details = {'exe': 'setting.exe', 'process_created': 1.0, 'process_key': '42:1000', 'process_ancestors': []}
        with mock.patch('dsh_desktop.decorations.is_decoration_window', return_value=False), \
                mock.patch.object(desktop.win32gui, 'IsWindowVisible', return_value=True), \
                mock.patch.object(desktop, '_safe_window_title', return_value=''), \
                mock.patch.object(desktop, '_visible_child_text', return_value='日志与信任管理'), \
                mock.patch.object(desktop.win32gui, 'GetClassName', return_value='Panel'), \
                mock.patch.object(desktop.win32gui, 'GetWindow', return_value=0), \
                mock.patch.object(desktop.win32gui, 'GetWindowRect', return_value=(0, 0, 100, 100)), \
                mock.patch.object(desktop.win32process, 'GetWindowThreadProcessId', return_value=(1, 42)), \
                mock.patch.object(desktop, '_process_details', return_value=details):
            row = desktop._window_info(12)
            self.assertEqual((row['title'], row['title_source'], row['window_text']), ('日志与信任管理', 'child_text', ''))
            self.assertEqual((row['hwnd'], row['pid'], row['process']), (12, 42, 'setting.exe'))
            self.assertIsNone(desktop._describe_window(12, include_untitled=False))
            with mock.patch.object(desktop, '_visible_child_text', return_value=''):
                row = desktop._window_info(12)
                self.assertEqual((row['title'], row['title_source']), ('Panel setting.exe', 'class_name'))
            with mock.patch.object(desktop, '_safe_window_title', return_value='Real title'):
                self.assertEqual(desktop._window_info(12)['title_source'], 'window_text')

    def test_child_title_uses_only_first_visible_text_with_visit_bound(self):
        def enumerate_children(hwnd, callback, value):
            for child in range(100):
                if callback(child, value) is False:
                    break
        with mock.patch.object(desktop.win32gui, 'EnumChildWindows', side_effect=enumerate_children), \
                mock.patch.object(desktop.win32gui, 'IsWindowVisible', side_effect=lambda child: child > 0), \
                mock.patch.object(desktop, '_safe_window_title', return_value='visible') as title:
            self.assertEqual(desktop._visible_child_text(12), 'visible')
            title.assert_called_once_with(1)
        with mock.patch.object(desktop.win32gui, 'EnumChildWindows', side_effect=enumerate_children), \
                mock.patch.object(desktop.win32gui, 'IsWindowVisible', return_value=True), \
                mock.patch.object(desktop, '_safe_window_title', return_value='') as title:
            self.assertEqual(desktop._visible_child_text(12), '')
            self.assertLessEqual(title.call_count, 16)

    def test_relationship_requires_process_instance_or_owner_identity(self):
        old = {'window_key': '10:42:1000', 'process_key': '42:1000', 'process_ancestors': ['1:1']}
        cases = [({'process_key': '43:2000', 'process_ancestors': ['42:1000', '1:1']}, 'child_process'),
                 ({'process_key': '42:1000'}, 'same_process'),
                 ({'owner_keys': ['10:42:1000']}, 'owner_chain'),
                 ({'process_key': '43:2000', 'process_ancestors': ['42:9999', '1:1']}, None),
                 ({'process_key': '44:3000', 'process_ancestors': ['1:1']}, None)]
        for candidate, expected in cases:
            with self.subTest(candidate=candidate):
                self.assertEqual(DesktopRuntime._window_relationship(old, candidate), expected)
        old.update(owner_keys=['9:40:900'], owner_process_keys=['40:900'])
        self.assertEqual(DesktopRuntime._window_relationship(old, {'owner_keys': ['9:40:900']}), 'owner_chain')
        self.assertEqual(DesktopRuntime._window_relationship(old, {'process_ancestors': ['40:900']}), 'child_process')

    def test_candidates_exclude_existing_unrelated_and_return_ambiguous_choices(self):
        runtime = mock.Mock()
        runtime._window_relationship.side_effect = DesktopRuntime._window_relationship
        old = SimpleNamespace(kind='window', target_id='old', metadata={'process_key': '42:1000'})
        related = {'kind': 'window', 'target_id': 'new', 'window_key': '11:43:2000', 'process': 'child.exe',
                   'title': '日志', 'title_source': 'child_text', 'hwnd': 11, 'pid': 43,
                   'process_key': '43:2000', 'process_ancestors': ['42:1000']}
        rows = [related, {**related, 'window_key': '12:43:2000', 'target_id': 'existing', 'hwnd': 12},
            {**related, 'window_key': '13:44:3000', 'target_id': 'unrelated', 'hwnd': 13, 'pid': 44,
             'process_key': '44:3000', 'process_ancestors': ['1:1']}]
        runtime._native_metadata.side_effect = lambda row: row
        runtime.targets.adopt.side_effect = lambda owner, kind, token, hwnd, row: SimpleNamespace(
            target_id=row['target_id'], summary=lambda: row)
        with mock.patch.object(desktop, 'window_identity_snapshot', return_value={row['hwnd']: row for row in rows}), \
                mock.patch.object(desktop, '_process_details', side_effect=lambda pid, **kwargs: next(row for row in rows if row['pid'] == pid)), \
                mock.patch.object(desktop, '_window_info', side_effect=lambda hwnd: next(row for row in rows if row['hwnd'] == hwnd)):
            candidates = DesktopRuntime.new_window_candidates(runtime, 'a', old, {'12:43:2000'})
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]['relationship'], 'child_process')
        self.assertEqual(candidates[0]['hwnd'], 11)
        self.assertIs(DesktopRuntime.rebind_window(runtime, 'a', old, candidates), runtime.resolve_target.return_value)
        self.assertIsNone(DesktopRuntime.rebind_window(runtime, 'a', old, candidates * 2))

    def test_closed_readback_preserves_candidate_and_rebound_observation(self):
        runtime = mock.Mock()
        old = SimpleNamespace(kind='window', target_id='old')
        runtime.observations.observe.side_effect = [RuntimeFault('TARGET_CLOSED', 'closed'),
            {'observation': {'observation_id': 'new-observation'}}]
        runtime.rebind_window.return_value = SimpleNamespace(target_id='new')
        candidates = [{'target_id': 'new', 'hwnd': 12}]
        result = ActionDispatcher(runtime)._readback('a', 'old', None, action='click', target=old, candidates=candidates)
        self.assertTrue(result['closed'])
        self.assertTrue(result['rebound'])
        self.assertEqual(result['observation']['rebound_from'], 'old')
        runtime.rebind_window.assert_called_once_with('a', old, candidates=candidates)

    def test_directory_observation_preserves_native_identity_and_option(self):
        runtime = mock.Mock()
        runtime.target_directory.return_value = [{'target_id': 'desktop', 'kind': 'desktop'},
            {'target_id': 'window', 'kind': 'window', 'hwnd': 12, 'pid': 42, 'process': 'app.exe',
             'title': 'Panel', 'window_text': '', 'title_source': 'class_name', 'owner_hwnd': 11}]
        service = SimpleNamespace(runtime=runtime, validate_arguments=ObservationService.validate_arguments)
        result = ObservationService.observe(service, 'a')
        self.assertEqual(result['targets'][1]['hwnd'], 12)
        self.assertEqual(result['targets'][1]['window_text'], '')
        runtime.target_directory.assert_called_with('a', include_untitled=True)
        ObservationService.observe(service, 'a', include_untitled=False)
        runtime.target_directory.assert_called_with('a', include_untitled=False)
        for arguments in ({'include_untitled': 1}, {'target_id': 't', 'include_untitled': True},
                          {'cursor': 'o.0.0', 'include_untitled': False}):
            with self.assertRaises(ValueError):
                ObservationService.validate_arguments(arguments)
