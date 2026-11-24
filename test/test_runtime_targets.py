import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from dsh_desktop.geometry import FrameGeometry
from dsh_desktop.targets import TargetRegistry, FrameRegistry, RuntimeFault
from dsh_desktop.runtime import DesktopRuntime, RuntimeConfig
from dsh_desktop.game.capture import CaptureService
from dsh_desktop.window_identity import WindowIdentities
from dsh_desktop.browser import BrowserRuntime


class GeometryTests(unittest.TestCase):
    def test_physical_dpi_crop_scale_and_negative_origin_roundtrip(self):
        for dpi in (96, 144):
            for origin in ((0, 0), (-2560, -400)):
                with self.subTest(dpi=dpi, origin=origin):
                    geometry = FrameGeometry((*origin, 1920, 1080), (400, 200),
                                             crop=(100, 80, 800, 400), dpi=(dpi, dpi),
                                             client_rect=(origin[0] + 8, origin[1] + 31, 1904, 1041),
                                             virtual_origin=origin)
                    point = geometry.to_screen(200, 100)
                    self.assertEqual(point, (origin[0] + 500, origin[1] + 280))
                    self.assertEqual(geometry.from_screen(*point), (200, 100))

    def test_invalid_points_crop_and_last_pixel(self):
        geometry = FrameGeometry((-50, 10, 10, 10), (100, 100))
        self.assertEqual(geometry.to_screen(99, 99), (-41, 19))
        for point in ((100, 0), (-1, 1), (float('nan'), 0), (True, 0)):
            with self.assertRaises(ValueError):
                geometry.to_screen(*point)
        with self.assertRaises(ValueError):
            FrameGeometry((0, 0, 10, 10), (1, 1), crop=(9, 9, 2, 2))

    def test_whole_window_and_client_fallback_expose_actual_source(self):
        service = CaptureService()
        metadata = {'window_rect': (-100, 20, 100, 80), 'client_rect': (-92, 50, 84, 42),
                    'dpi': (144, 144), 'virtual_origin': (-2560, 0)}
        with mock.patch('dsh_desktop.game.capture.window_geometry', return_value=metadata):
            with mock.patch.object(service, 'print_window', return_value=np.full((80, 100, 3), 127, dtype=np.uint8)):
                result = service.capture_detailed(42)
                self.assertEqual(result.geometry.to_screen(50, 40), (-50, 60))
                self.assertEqual(result.geometry.method, 'printwindow')
            with mock.patch.object(service, 'print_window', return_value=None), \
                 mock.patch('dsh_desktop.game.capture.mss.mss', side_effect=RuntimeError('locked')), \
                 mock.patch.object(service, '_capture_print_window', return_value=np.ones((42, 84, 3), dtype=np.uint8)):
                result = service.capture_detailed(42)
                self.assertEqual(result.geometry.source_rect, metadata['client_rect'])
                self.assertEqual(result.geometry.to_screen(0, 0), (-92, 50))
                self.assertEqual(result.geometry.method, 'printwindow_client')


class TargetTests(unittest.TestCase):
    def test_popup_discovery_inherits_opener_owner_and_skips_unrelated_tabs(self):
        from types import SimpleNamespace
        registry = TargetRegistry('fixture')
        runtime = SimpleNamespace(targets=registry, epoch='fixture')
        browser = BrowserRuntime(runtime)
        opener, popup, unrelated = mock.Mock(), mock.Mock(), mock.Mock()
        for page in (opener, popup, unrelated):
            page.is_closed.return_value = False
        popup.opener.return_value = opener
        unrelated.opener.return_value = None
        context = mock.Mock(pages=[opener, popup, unrelated])
        opener.context = context
        browser._page_ids[opener] = 'original'
        browser._owned_pages[opener] = 'a'
        registry.adopt('a', 'browser', 'original', (opener, opener.main_frame), {})
        with mock.patch.object(browser, '_adopt') as adopt:
            browser.discover('b')
            adopt.assert_not_called()
            browser.discover('a')
            adopt.assert_called_once_with('a', popup)
        self.assertEqual(browser._owned_pages[popup], 'a')

    def test_failed_navigation_retry_retains_target_and_dispatches_once(self):
        runtime = mock.Mock(epoch='fixture')
        browser = BrowserRuntime(runtime)
        target = mock.Mock(target_id='fixture')
        target.handle = (mock.Mock(), mock.Mock())
        target.handle[1].goto.side_effect = RuntimeError('timed out')
        browser.resolve = mock.Mock(return_value=target)
        for _ in range(2):
            with self.assertRaises(RuntimeFault) as error:
                browser._open('a', 'http://fixture', 'call', 'fixture')
            self.assertEqual(error.exception.code, 'NAVIGATION_FAILED')
        target.handle[1].goto.assert_called_once()

    def test_existing_profile_is_used_and_profile_failure_is_diagnostic(self):
        from dsh_desktop import browser_support as web
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            legacy = home / 'web_profile_edge'
            legacy.mkdir()
            runtime = mock.Mock(home=home, state_dir=home / 'state', epoch='fixture')
            browser = BrowserRuntime(runtime)
            driver = mock.Mock()
            with mock.patch.object(browser, '_driver', return_value=driver), \
                 mock.patch.object(browser, '_acquire_profile'), \
                 mock.patch.object(web, '_browser_mode', return_value='edge'), \
                 mock.patch.object(web, '_prepare_playwright_temp_dir'), \
                 mock.patch.object(web, '_launch_persistent_context', side_effect=RuntimeError('profile is in use')), \
                 mock.patch.dict('os.environ', {}, clear=False):
                with self.assertRaises(RuntimeFault) as error:
                    browser._context()
                self.assertEqual(error.exception.code, 'BROWSER_PROFILE_UNAVAILABLE')
                self.assertEqual(browser.profile, legacy)
                self.assertEqual(web._profile_dir(), legacy)

    def test_owner_epoch_and_revisions(self):
        registry = TargetRegistry('epoch')
        a = registry.adopt('a', 'window', 'same', 1, {'rect': [0, 0, 20, 30]})
        b = registry.adopt('b', 'window', 'same', 1, {'rect': [0, 0, 20, 30]})
        self.assertNotEqual(a.target_id, b.target_id)
        self.assertEqual(a.revision, 1)
        self.assertIs(registry.adopt('a', 'window', 'same', 1, a.metadata), a)
        registry.refresh(a, {'rect': [-30, 10, 40, 50]})
        self.assertEqual(a.revision, 2)
        with self.assertRaisesRegex(RuntimeFault, 'session'):
            registry.get('b', a.target_id)
        with self.assertRaises(RuntimeFault) as error:
            TargetRegistry('new').get('a', a.target_id)
        self.assertEqual(error.exception.code, 'RUNTIME_RESTARTED')
        registry.release('a')
        self.assertIs(registry.get('b', b.target_id), b)

    def test_frame_owner_target_revision_expiry_and_capacity(self):
        targets = TargetRegistry('epoch')
        target = targets.adopt('a', 'window', 1, 1, {'x': 0})
        frames = FrameRegistry('epoch', limit=2, ttl=10)
        geometry = FrameGeometry((0, 0, 100, 100), (50, 50))
        old = frames.add(target, geometry)
        frames.add(target, geometry)
        current = frames.add(target, geometry)
        with self.assertRaises(RuntimeFault):
            frames.get('a', old.frame_id, target)
        with self.assertRaises(RuntimeFault):
            frames.get('b', current.frame_id, target)
        targets.refresh(target, {'x': 1})
        concurrent = frames.add(target, geometry, revision=1)
        self.assertEqual(concurrent.revision, 1)
        with self.assertRaises(RuntimeFault) as error:
            frames.get('a', current.frame_id, target)
        self.assertEqual(error.exception.code, 'FRAME_STALE')
        with mock.patch('dsh_desktop.targets.time.monotonic', return_value=current.created + 11):
            with self.assertRaises(RuntimeFault):
                frames.get('a', current.frame_id, target)
        self.assertEqual(len(frames._frames), 0)

    def test_same_process_hwnd_reuse_loses_window_property(self):
        identities = WindowIdentities('fixture')
        identities._api = mock.Mock()
        identities._api.GetPropW.return_value = None
        identities._api.SetPropW.return_value = True
        first = identities.acquire(42, '42:100:1234')
        identities._api.GetPropW.return_value = first[1]
        self.assertTrue(identities.matches(42, first, '42:100:1234'))
        identities._api.GetPropW.return_value = None
        self.assertFalse(identities.matches(42, first, '42:100:1234'))
        second = identities.acquire(42, '42:100:1234')
        self.assertNotEqual(first, second)

    def test_runtime_mapping_revalidates_live_window(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = DesktopRuntime(RuntimeConfig.from_home(Path(directory)), home=directory, state_dir=directory)
            info = {'hwnd': 42, 'window_key': '42:100:1234', 'title': 'fixture'}
            metadata = {'window_rect': (10, 20, 100, 100), 'client_rect': (18, 50, 84, 62),
                        'dpi': (96, 96), 'virtual_origin': (0, 0)}
            with mock.patch('dsh_desktop.desktop_tools._window_info', return_value=info), \
                 mock.patch('dsh_desktop.game.capture.window_geometry', return_value=metadata), \
                 mock.patch.object(runtime.window_identities, 'acquire', return_value=('42:100:1234', 1)), \
                 mock.patch.object(runtime.window_identities, 'matches', return_value=True):
                target = runtime.open_target('a', hwnd=42)
                frame = runtime.frames.add(target, FrameGeometry((10, 20, 100, 100), (50, 50)))
                self.assertEqual(runtime.map_point('a', target.target_id, frame.frame_id, 25, 25), (60, 70))
                metadata['dpi'] = (144, 144)
                with self.assertRaises(RuntimeFault) as error:
                    runtime.map_point('a', target.target_id, frame.frame_id, 25, 25)
                self.assertEqual(error.exception.code, 'FRAME_STALE')
                with mock.patch.object(runtime.window_identities, 'matches', return_value=False):
                    with self.assertRaises(RuntimeFault) as error:
                        runtime.resolve_target('a', target.target_id)
                    self.assertEqual(error.exception.code, 'TARGET_CLOSED')


if __name__ == '__main__':
    unittest.main()
