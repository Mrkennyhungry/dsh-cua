from concurrent.futures import ThreadPoolExecutor
import threading
import time
import unittest
import uuid
import numpy as np
from types import SimpleNamespace
from unittest import mock

from dsh_desktop.geometry import FrameGeometry
from dsh_desktop.observations import Element, Observation, ObservationService
from dsh_desktop.targets import TargetRegistry, FrameRegistry, RuntimeFault
from dsh_desktop.worker import ApartmentWorker


class ObservationTests(unittest.TestCase):
    def test_uia_expand_collapse_state_is_optional(self):
        from dsh_desktop.desktop_tools import _uia_batch_elements

        element = mock.Mock()
        element.element_info.name = 'Menu'
        element.element_info.automation_id = 'menu'
        element.element_info.control_type = 'MenuItem'
        element.element_info.runtime_id = (1, 2)
        element.element_info.has_keyboard_focus = False
        element.element_info.rectangle = SimpleNamespace(left=0, top=0, right=20, bottom=20,
                                                          width=lambda: 20, height=lambda: 20)
        element.children.return_value = []
        element.iface_expand_collapse.CurrentExpandCollapseState = 1
        region = SimpleNamespace(left=0, top=0, width=100, height=100)
        nodes = _uia_batch_elements(element, region, 2, 10, time.monotonic() + 2, semantics=True)
        self.assertTrue(nodes[0]['expanded'])
        element.iface_expand_collapse.CurrentExpandCollapseState = 2
        nodes = _uia_batch_elements(element, region, 2, 10, time.monotonic() + 2, semantics=True)
        self.assertNotIn('expanded', nodes[0])

    def test_native_duplicate_prefers_control_inside_client_region(self):
        from dsh_desktop import desktop_tools

        outside = {'name': 'Search', 'control_type': 'Edit', 'rect': (120, 150, 40, 20),
                   'wrapper': mock.Mock(), 'value': '', 'enabled': True}
        visible = {'name': 'Search', 'control_type': 'Edit', 'rect': (120, 220, 40, 20),
                   'wrapper': mock.Mock(), 'value': '', 'enabled': True}
        root = mock.Mock()
        target = SimpleNamespace(handle=42)
        self.runtime.resolve_target.return_value = target
        self.runtime.window.get_client_region.return_value = SimpleNamespace(left=100, top=200,
                                                                               width=300, height=200)
        with mock.patch.dict('sys.modules', {'pywinauto': SimpleNamespace(Desktop=mock.Mock())}) as modules:
            modules['pywinauto'].Desktop.return_value.window.return_value.wrapper_object.return_value = root
            with mock.patch.object(desktop_tools, '_uia_batch_elements', return_value=[outside, visible]):
                elements, text, _metadata = self.service._native('a', self.target.target_id, {'uia': 0})

        self.assertEqual(text, 'Search')
        self.assertEqual(len(elements), 1)
        token = elements[0].locator['token']
        self.assertIs(self.service._uia_elements[token][2], visible)

    def test_directory_preserves_tab_identity_for_same_url_frames(self):
        self.runtime.target_directory.return_value = [
            {'target_id': 'desktop', 'kind': 'desktop'},
            {'target_id': 'first', 'kind': 'browser', 'url': 'http://fixture', 'tab_id': 'tab-a', 'frame_name': ''},
            {'target_id': 'second', 'kind': 'browser', 'url': 'http://fixture', 'tab_id': 'tab-b', 'frame_name': 'second'},
        ]
        targets = self.service.observe('a')['targets']
        self.assertEqual(targets[1]['tab_id'], 'tab-a')
        self.assertEqual(targets[2]['tab_id'], 'tab-b')

    def test_closed_target_observation_cannot_be_used_on_replacement(self):
        observation = self.snapshot()
        self.runtime.resolve_target.side_effect = RuntimeFault('TARGET_CLOSED', 'closed')
        with self.assertRaises(RuntimeFault) as error:
            self.service.get('a', observation.observation_id, 'replacement')
        self.assertEqual(error.exception.code, 'TARGET_STALE')

    def test_uia_timeout_falls_back_to_bounded_window_ocr(self):
        self.runtime.resolve_target.return_value = self.target
        frame = self.runtime.frames.add(self.target, FrameGeometry((10, 20, 100, 100), (100, 100)))
        self.runtime.capture_target.return_value = (np.zeros((100, 100, 3), dtype=np.uint8), frame)
        self.runtime._popup_ocr_worker.call.return_value = [
            {'text': '日志', 'confidence': 0.8, 'bbox': [100, 200, 400, 400]}]
        with mock.patch.object(self.service.uia, 'call', side_effect=RuntimeFault('BACKEND_TIMEOUT', 'timeout')):
            observed = self.service.observe('a', self.target.target_id, image=False)['observation']
        self.assertEqual(observed['uia_coverage'], 'timeout')
        self.assertEqual(observed['text'], '日志')
        self.assertEqual(observed['elements'][0]['source'], 'ocr')
        self.assertEqual(observed['elements'][0]['raw_text'], '日志')
        self.assertNotIn('name', observed['elements'][0])
        self.assertTrue(observed['elements'][0]['ref'].startswith('v'))
        self.assertEqual(len(observed['frames']), 1)
        self.assertEqual(self.runtime._popup_ocr_worker.call.call_args.kwargs['timeout'], 3)

    def test_transient_ref_expires_before_dispatch(self):
        observation = self.snapshot()
        observation.elements[0].locator['expires_at'] = time.monotonic() - 1
        with self.assertRaises(RuntimeFault) as error:
            self.service.element('a', observation.observation_id, self.target.target_id, 'e0')
        self.assertEqual(error.exception.code, 'REF_STALE')

    def test_explicit_visual_sources_bypass_uia_and_combined_keeps_structured_state(self):
        self.service.image_budget = 100000
        self.runtime.resolve_target.return_value = self.target
        self.runtime.capture_target.side_effect = lambda *args, **kwargs: (
            np.zeros((100, 100, 3), dtype=np.uint8),
            self.runtime.frames.add(self.target, FrameGeometry((10, 20, 100, 100), (100, 100))))
        self.runtime._popup_ocr_worker.call.return_value = [
            {'text': 'Visible page body', 'confidence': 0.95, 'bbox': [0, 0, 500, 100]}]
        node = Element('', 'uia', self.target.target_id, None,
                       {'name': 'Virus', 'role': 'tab', 'selected': True}, {}, (10, 20, 20, 10))
        with mock.patch.object(self.service.uia, 'call', return_value=([node], 'Virus', {})) as uia:
            for source in ('ocr', 'image'):
                observed = self.service.observe('a', self.target.target_id, source=source)['observation']
                self.assertEqual(observed['uia_coverage'], 'not_requested')
                self.assertEqual(len(observed['frames']), 1)
                self.assertEqual(bool(observed['elements']), source == 'ocr')
            uia.assert_not_called()
            observed = self.service.observe('a', self.target.target_id, source='combined')['observation']
            self.assertTrue(observed['elements'][0]['selected'])
            self.assertIn('Visible page body', observed['text'])
            self.assertEqual(observed['elements'][1]['source'], 'ocr')
            self.assertTrue(observed['elements'][1]['semantic_missing'])
            self.assertEqual(observed['uia_coverage'], 'partial')
            self.assertEqual(observed['trace']['yolo'], 0)
            self.assertEqual(uia.call_count, 1)
        with self.assertRaisesRegex(ValueError, 'target_id'):
            self.service.validate_arguments({'source': 'ocr'})
        with self.assertRaisesRegex(ValueError, 'source'):
            self.service.observe('a', self.target.target_id, source='invalid')
        with self.assertRaisesRegex(ValueError, 'Cursor'):
            self.service.observe('a', self.target.target_id, cursor='o.0.0', source='image')

    def test_popup_ocr_filters_existing_text_and_expires_new_candidates(self):
        self.service.image_budget = 100000
        geometry = FrameGeometry((10, 20, 100, 100), (100, 100))
        self.runtime.capture_target.side_effect = [
            (np.zeros((100, 100, 3), dtype=np.uint8), self.runtime.frames.add(self.target, geometry)),
            (np.zeros((100, 100, 3), dtype=np.uint8), self.runtime.frames.add(self.target, geometry))]
        before = self.service.capture_frame('a', self.target.target_id)
        after = self.service.capture_frame('a', self.target.target_id)
        existing = {'text': '日志', 'bbox': [100, 100, 300, 200], 'confidence': 0.98}
        menu = {'text': '病毒查杀日志', 'bbox': [100, 300, 600, 400], 'confidence': 0.96}
        self.runtime.perceive.side_effect = [[existing], [existing, menu]]
        self.runtime._popup_ocr_worker.call.side_effect = lambda fn, **kwargs: fn()
        region = {'x': 0, 'y': 0, 'width': 100, 'height': 100}
        candidates = self.service.popup_candidates('a', before, after, region)
        self.assertEqual([item['raw_text'] for item in candidates], ['病毒查杀日志'])
        self.assertEqual(candidates[0]['ref'], 'p1')
        self.assertFalse(candidates[0]['appearance_verified'])
        self.assertEqual(candidates[0]['ttl_ms'], 30000)
        self.assertEqual(self.runtime._popup_ocr_worker.call.call_args.kwargs['timeout'], 6)
        self.assertEqual(self.service.element('a', after['observation_id'], self.target.target_id, 'p1')[0].source, 'popup_ocr')

    def setUp(self):
        self.runtime = mock.Mock()
        self.runtime.frames = FrameRegistry('fixture')
        self.target = TargetRegistry('fixture').adopt('a', 'window', 1, 42, {'title': 'fixture'})
        self.service = ObservationService(self.runtime, limit=2, ttl=10, image_budget=100)

    def test_evidence_crop_reuses_source_pixels_and_preserves_scaled_geometry(self):
        import cv2

        self.service.image_budget = 100000
        geometry = FrameGeometry((-300, 20, 200, 200), (100, 100))
        self.runtime.resolve_target.return_value = self.target
        self.runtime.capture_target.return_value = (np.full((100, 100, 3), 230, dtype=np.uint8),
                                                   self.runtime.frames.add(self.target, geometry))
        source = self.service.capture_frame('a', self.target.target_id)
        region = {'x': 10, 'y': 20, 'width': 40, 'height': 30}
        cropped = self.service.crop_frame('a', source, region)
        self.assertEqual(self.runtime.capture_target.call_count, 1)
        self.assertEqual((cropped['width'], cropped['height']), (40, 30))
        self.assertEqual(cropped['source_frame_id'], source['frame_id'])
        self.assertFalse(cropped['clickable'])
        frame = self.runtime.frames.get('a', cropped['frame_id'], self.target)
        self.assertEqual(frame.geometry.to_screen(0, 0), (-280, 60))
        pixels = cv2.imdecode(np.frombuffer(self.service.image('a', cropped['frame_id']), np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(pixels.shape, (30, 40, 3))
        with self.assertRaises(RuntimeFault):
            self.service.image('b', cropped['frame_id'])
        with self.assertRaises(RuntimeFault):
            self.service.crop_frame('a', source, {**region, 'width': 200})
        self.service.release_target('a', self.target.target_id)
        with self.assertRaises(RuntimeFault):
            self.service.image('a', cropped['frame_id'])

    def snapshot(self, owner='a', image_size=5):
        frame = self.runtime.frames.add(self.target, FrameGeometry((10, 20, 100, 100), (50, 50)))
        elements = [Element(f'e{index}', 'dom', self.target.target_id, 'document',
                            {'name': '中文' + str(index), 'role': 'button', 'checked': True},
                            {'selector': 'x' * 500, 'token': str(index)}, (20, 30, 40, 40)) for index in range(100)]
        observation = Observation('o_' + uuid.uuid4().hex, owner, self.target.target_id, 1,
                                  'fixed-time', elements, '正文' * 4000, {}, [frame], {frame.frame_id: b'x' * image_size})
        self.service._snapshots[observation.observation_id] = observation
        return observation

    def test_pagination_pins_snapshot_and_removes_unsent_image_bounds(self):
        observation = self.snapshot()
        first = self.service._page(observation)['observation']
        self.assertEqual(len(first['elements']), 80)
        self.assertEqual(len(first['text']), 6000)
        self.assertIn('bounds', first['elements'][0])
        second = self.service.observe('a', cursor=first['next_cursor'])['observation']
        self.assertEqual(second['observation_id'], observation.observation_id)
        self.assertEqual(second['captured_at'], 'fixed-time')
        self.assertEqual(len(second['elements']), 20)
        self.assertEqual(len(second['text']), 2000)
        self.assertNotIn('bounds', second['elements'][0])
        self.assertEqual(second['frames'], [])
        self.assertNotIn('next_cursor', second)
        with self.assertRaises(RuntimeFault):
            self.service.observe('b', cursor=first['next_cursor'])
        with self.assertRaises(ValueError):
            self.service.observe('a', cursor=first['next_cursor'], image=True)
        with self.assertRaises(ValueError):
            self.service.observe('a', cursor=f'{observation.observation_id}.10000.0')

    def test_expiry_capacity_and_image_budget_release_associated_frames(self):
        first = self.snapshot()
        second = self.snapshot()
        third = self.snapshot()
        self.service._prune()
        self.assertEqual(len(self.service._snapshots), 2)
        self.assertNotIn(first.frames[0].frame_id, self.runtime.frames._frames)
        self.assertEqual(self.service.image('a', third.frames[0].frame_id), b'xxxxx')
        self.snapshot(image_size=99)
        self.service._prune()
        self.assertEqual(len(self.service._snapshots), 1)
        with self.assertRaises(RuntimeFault):
            self.service.get('a', second.observation_id)
        remaining = next(iter(self.service._snapshots.values()))
        with mock.patch('dsh_desktop.observations.time.monotonic', return_value=remaining.created + 11):
            with self.assertRaises(RuntimeFault):
                self.service.image('a', remaining.frames[0].frame_id)
        self.assertEqual(len(self.runtime.frames._frames), 0)

    def test_owner_release_and_public_truncation_preserve_internal_locator(self):
        owned = self.snapshot()
        other = self.snapshot(owner='b')
        element = owned.elements[0]
        element.semantic['value'] = '长文本' * 1000
        element.semantic['options'] = ['选择' + str(index) for index in range(25)]
        public = element.public(owned.frames[0])
        self.assertEqual(len(public['value']), 1000)
        self.assertTrue(public['options_truncated'])
        self.assertEqual(len(element.locator['selector']), 500)
        self.assertEqual(len(element.semantic['value']), 3000)
        self.service.release('a')
        self.assertIs(self.service.get('b', other.observation_id), other)
        with self.assertRaises(RuntimeFault):
            self.service.get('a', owned.observation_id)

    def test_crop_uses_parent_image_scale_and_origin(self):
        frame = self.runtime.frames.add(self.target, FrameGeometry((-100, 20, 1000, 500), (200, 100), crop=(100, 50, 400, 200)))
        crop = self.service._crop('a', self.target, {'frame_id': frame.frame_id, 'x': 50, 'y': 20, 'width': 100, 'height': 50})
        self.assertEqual(crop, (200, 90, 200, 100))


class WorkerTests(unittest.TestCase):
    def test_apartment_startup_accepts_two_calls_with_bounded_admission(self):
        import pythoncom
        initialize = pythoncom.CoInitialize
        entered, release = threading.Event(), threading.Event()
        worker = ApartmentWorker('startup-uia')
        def delayed_initialize():
            entered.set()
            release.wait(2)
            initialize()
        try:
            with mock.patch.object(pythoncom, 'CoInitialize', side_effect=delayed_initialize):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    first = pool.submit(worker.call, lambda: 1)
                    self.assertTrue(entered.wait(1))
                    second = pool.submit(worker.call, lambda: 2)
                    deadline = time.monotonic() + 1
                    while worker._queue.qsize() < 2 and time.monotonic() < deadline:
                        time.sleep(0.001)
                    self.assertEqual(worker._queue.qsize(), 2)
                    with self.assertRaises(RuntimeFault) as error:
                        worker.call(lambda: 3)
                    self.assertEqual(error.exception.code, 'BACKEND_BUSY')
                    release.set()
                    self.assertEqual([first.result(timeout=1), second.result(timeout=1)], [1, 2])
        finally:
            release.set()
            worker.shutdown()

    def test_blocked_provider_uses_one_thread_and_one_bounded_queue(self):
        worker = ApartmentWorker('test-uia')
        started, release = threading.Event(), threading.Event()
        def blocked():
            started.set()
            release.wait(2)
            return 42
        try:
            with self.assertRaises(RuntimeFault) as error:
                worker.call(blocked, timeout=0.02)
            self.assertEqual(error.exception.code, 'BACKEND_TIMEOUT')
            self.assertTrue(started.wait(1))
            with ThreadPoolExecutor(max_workers=1) as pool:
                queued = pool.submit(worker.call, lambda: 7, timeout=1)
                deadline = time.monotonic() + 1
                while worker._queue.empty() and time.monotonic() < deadline:
                    time.sleep(0.001)
                with self.assertRaises(RuntimeFault) as error:
                    worker.call(lambda: 0)
                self.assertEqual(error.exception.code, 'BACKEND_BUSY')
                self.assertEqual(sum(thread.name == 'test-uia' for thread in threading.enumerate()), 1)
                release.set()
                self.assertEqual(queued.result(timeout=1), 7)
        finally:
            release.set()
            worker.shutdown()
        self.assertFalse(worker._thread.is_alive())


if __name__ == '__main__':
    unittest.main()
