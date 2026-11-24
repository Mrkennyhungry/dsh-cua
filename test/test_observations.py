from concurrent.futures import ThreadPoolExecutor
import threading
import time
import unittest
import uuid
from unittest import mock

from dsh_desktop.geometry import FrameGeometry
from dsh_desktop.observations import Element, Observation, ObservationService
from dsh_desktop.targets import TargetRegistry, FrameRegistry, RuntimeFault
from dsh_desktop.worker import ApartmentWorker


class ObservationTests(unittest.TestCase):
    def test_directory_preserves_tab_identity_for_same_url_frames(self):
        self.runtime.target_directory.return_value = [
            {'target_id': 'desktop', 'kind': 'desktop'},
            {'target_id': 'first', 'kind': 'browser', 'url': 'http://fixture', 'tab_id': 'tab-a', 'frame_name': ''},
            {'target_id': 'second', 'kind': 'browser', 'url': 'http://fixture', 'tab_id': 'tab-b', 'frame_name': 'second'},
        ]
        targets = self.service.observe('a')['targets']
        self.assertEqual(targets[1]['tab_id'], 'tab-a')
        self.assertEqual(targets[2]['tab_id'], 'tab-b')

    def setUp(self):
        self.runtime = mock.Mock()
        self.runtime.frames = FrameRegistry('fixture')
        self.target = TargetRegistry('fixture').adopt('a', 'window', 1, 42, {'title': 'fixture'})
        self.service = ObservationService(self.runtime, limit=2, ttl=10, image_budget=100)

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
