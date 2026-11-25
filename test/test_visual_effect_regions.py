"""Small action feedback must survive window-size dilution and remain traceable."""
from contextlib import nullcontext
import hashlib
import json
from types import SimpleNamespace
from unittest import mock
import unittest

import cv2
import numpy as np

from dsh_desktop.actions import ActionDispatcher
from dsh_desktop.geometry import FrameGeometry
from dsh_desktop.targets import Frame


def jpeg(image):
    ok, encoded = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 85])
    assert ok
    return encoded.tobytes()


class LocalVisualEffectTests(unittest.TestCase):
    def setUp(self):
        self.dispatcher = ActionDispatcher(mock.Mock())
        self.before = np.full((800, 1200, 3), 80, dtype=np.uint8)
        self.frame = Frame('before-frame', 'owner', 'target', 1,
                           FrameGeometry((0, 0, 1200, 800), (1200, 800))).summary()
        self.location = {'point': (720, 740), 'rect': (704, 724, 32, 32)}
        self.regions = self.dispatcher._visual_regions(self.frame, self.location)

    def compare(self, after, regions=None):
        return self.dispatcher._compare_visual_effect(
            jpeg(self.before), jpeg(after), self.regions if regions is None else regions)

    def test_small_play_icon_change_is_detected_locally(self):
        after = self.before.copy()
        after[734:746, 716:720] = 240
        after[734:746, 724:728] = 240
        result = self.compare(after)
        self.assertTrue(result['visual_effect'])
        self.assertFalse(result['visual_effect_global_changed'])
        self.assertLess(result['visual_effect_changed_ratio'], 0.005)
        self.assertTrue(result['visual_effect_local_changed'])
        self.assertEqual(result['visual_effect_basis'], 'local')
        self.assertTrue(result['visual_effect_regions'][0]['changed'])
        self.assertNotIn('visual_effect_attribution', result)
        self.assertEqual(json.loads(json.dumps(result)), result)

    def test_same_icon_change_is_detected_in_larger_window(self):
        self.before = cv2.resize(self.before, (2400, 1600))
        after = self.before.copy()
        after[734:746, 716:720] = 240
        self.assertTrue(self.compare(after)['visual_effect_local_changed'])

    def test_identical_frames_and_jpeg_noise_are_false(self):
        for after in (self.before, self.before + 2):
            with self.subTest(value=int(after[0, 0, 0])):
                result = self.compare(after)
                self.assertFalse(result['visual_effect'])
                self.assertFalse(result['visual_effect_local_changed'])
                self.assertNotIn('visual_effect_attribution', result)

    def test_tiny_caret_noise_is_below_local_threshold(self):
        after = self.before.copy()
        after[735:743, 719:720] = 255
        self.assertFalse(self.compare(after)['visual_effect'])

    def test_minimum_pixel_count_rejects_single_pixel_in_tiny_region(self):
        after = self.before.copy()
        after[740, 720] = 255
        result = self.compare(after, [{'basis': 'element_bbox', 'bbox': [718, 738, 722, 742]}])
        self.assertGreater(result['visual_effect_regions'][0]['changed_ratio'], 0.01)
        self.assertFalse(result['visual_effect_local_changed'])

    def test_distant_banner_is_reported_as_global_only(self):
        after = self.before.copy()
        after[100:300, 200:1000] = 240
        result = self.compare(after)
        self.assertTrue(result['visual_effect'])
        self.assertEqual(result['visual_effect_basis'], 'full_frame')
        self.assertFalse(result['visual_effect_local_changed'])
        self.assertEqual(result['visual_effect_attribution'], 'unknown')
        self.assertEqual(result['visual_effect_attribution_reason'], 'full_frame_only')

    def test_dropdown_change_outside_small_neighborhood_reaches_surroundings(self):
        # Use an upward-opening menu at a button away from the image edge.
        after = self.before.copy()
        after[570:595, 705:740] = 240
        regions = self.dispatcher._visual_regions(self.frame, {'point': (720, 650)})
        result = self.compare(after, regions)
        self.assertFalse(result['visual_effect_regions'][0]['changed'])
        self.assertTrue(result['visual_effect_regions'][1]['changed'])
        self.assertFalse(result['visual_effect_global_changed'])
        self.assertTrue(result['visual_effect'])
        self.assertEqual(result['visual_effect_attribution'], 'unknown')
        self.assertEqual(result['visual_effect_attribution_reason'], 'surroundings_only')
        self.assertIn('Verify the expected UI state', result['visual_effect_warning'])

    def test_global_and_surroundings_change_has_unknown_attribution(self):
        after = self.before.copy()
        after[570:615, 630:810] = 240
        regions = self.dispatcher._visual_regions(self.frame, {'point': (720, 650)})
        result = self.compare(after, regions)
        self.assertTrue(result['visual_effect_global_changed'])
        self.assertFalse(result['visual_effect_regions'][0]['changed'])
        self.assertTrue(result['visual_effect_regions'][1]['changed'])
        self.assertEqual(result['visual_effect_attribution_reason'], 'surroundings_only')

    def test_global_result_still_works_without_local_geometry(self):
        after = np.full_like(self.before, 240)
        result = self.compare(after, [])
        self.assertTrue(result['visual_effect_global_changed'])
        self.assertIsNone(result['visual_effect_local_changed'])
        self.assertEqual(result['visual_effect_regions'], [])
        self.assertEqual(result['visual_effect_attribution_reason'], 'full_frame_only')

    def test_scaled_negative_origin_and_cropped_frames_map_regions(self):
        geometry = FrameGeometry((-1600, -500, 1200, 800), (300, 200), crop=(100, 100, 600, 400))
        frame = Frame('crop', 'owner', 'target', 1, geometry).summary()
        regions = self.dispatcher._visual_regions(frame, {
            'point': (-1200, -200), 'rect': (-1220, -220, 40, 40)})
        self.assertEqual(regions[0]['bbox'], [128, 78, 172, 122])
        self.assertEqual(regions[1]['bbox'], [118, 68, 182, 132])
        self.assertEqual(self.dispatcher._visual_regions(frame, {'point': (0, 0)}), [])

    def test_browser_iframe_css_origin_is_respected(self):
        geometry = FrameGeometry((200, 100, 400, 300), (400, 300), coordinate_space='browser_css')
        frame = Frame('css', 'owner', 'target', 1, geometry).summary()
        regions = self.dispatcher._visual_regions(frame, {'point': (210, 110)})
        self.assertEqual(regions[0]['bbox'], [-22, -22, 42, 42])

    def test_edge_region_is_clipped_to_image(self):
        after = self.before.copy()
        after[0:12, 0:12] = 240
        regions = self.dispatcher._visual_regions(self.frame, {'point': (0, 0)})
        result = self.compare(after, regions)
        self.assertEqual(result['visual_effect_regions'][0]['bbox'], [0, 0, 32, 32])
        self.assertTrue(result['visual_effect_local_changed'])

    def readback_fixture(self, after_frame=None):
        runtime = mock.Mock()
        before = jpeg(self.before)
        after_pixels = self.before.copy()
        after_pixels[734:746, 716:728] = 240
        after = jpeg(after_pixels)
        runtime.observations.image.side_effect = [before, after]
        runtime.observations.observe.return_value = {'observation': {
            'observation_id': 'after', 'frames': [after_frame or {**self.frame, 'frame_id': 'after-frame'}]}}
        runtime.observations.get.return_value = SimpleNamespace(text='', elements=[], metadata={})
        return runtime, {'observation': {'observation_id': 'before', 'frames': [self.frame]}}, before, after

    def test_readback_records_exact_frame_identity_hashes_and_step(self):
        runtime, before_observation, before, after = self.readback_fixture()
        result = ActionDispatcher(runtime)._readback('owner', 'target', None,
            before_observation=before_observation, action='click', location=self.location, step_index=2)
        self.assertTrue(result['visual_effect_local_changed'])
        self.assertEqual(result['visual_effect_step'], 2)
        self.assertEqual(result['visual_effect_frames'], {
            'before_observation_id': 'before', 'after_observation_id': 'after',
            'before_frame_id': 'before-frame', 'after_frame_id': 'after-frame',
            'before_sha256': hashlib.sha256(before).hexdigest(), 'after_sha256': hashlib.sha256(after).hexdigest()})

    def test_equal_size_shifted_geometry_is_unknown(self):
        runtime, before, _, _ = self.readback_fixture({**self.frame, 'source_rect': [10, 0, 1200, 800]})
        result = ActionDispatcher(runtime)._readback('owner', 'target', None,
            before_observation=before, action='click', location=self.location)
        self.assertIsNone(result['visual_effect'])
        self.assertEqual(result['visual_effect_reason'], 'geometry_mismatch')
        self.assertNotIn('visual_effect_attribution', result)

    def test_missing_before_capture_is_explicitly_unknown(self):
        runtime, _, _, _ = self.readback_fixture()
        result = ActionDispatcher(runtime)._readback('owner', 'target', None, action='click')
        self.assertIsNone(result['visual_effect'])
        self.assertEqual(result['visual_effect_reason'], 'before_capture_unavailable')

    def test_physical_click_normal_batch_entry_uses_actual_mapped_point(self):
        runtime, before, _, _ = self.readback_fixture()
        runtime.observations.observe.side_effect = [before, runtime.observations.observe.return_value]
        runtime._input_lock = object()
        runtime.map_point.return_value = (720, 740)
        runtime.input.click_screen.return_value = {'success': True}
        runtime.input.dispatch_context.side_effect = lambda _guard: nullcontext()
        runtime.observations.get.return_value.frames = [SimpleNamespace(frame_id='source-frame')]
        target = SimpleNamespace(kind='desktop', target_id='target', metadata={})
        runtime.resolve_target.return_value = target
        context = mock.Mock(owner='owner', visual_location=None)
        context.hold.side_effect = lambda _lock: nullcontext()
        dispatcher = ActionDispatcher(runtime)
        dispatcher._boundary = mock.Mock(return_value=('stable',))
        result = dispatcher._batch(context, {'target_id': 'target', 'observation_id': 'source',
            'steps': [{'action': 'click', 'target': {'frame_id': 'source-frame', 'x': 360, 'y': 370}}]})
        self.assertTrue(result['visual_effect_local_changed'])
        self.assertEqual(result['visual_effect_step'], 0)
        self.assertEqual(result['visual_effect_regions'][0]['basis'], 'click_neighborhood')
        runtime.input.click_screen.assert_called_once_with(720, 740, clicks=1)

    def test_dom_and_uia_semantic_actions_record_control_bounds(self):
        context = mock.Mock(visual_location=None)
        node = mock.Mock()
        node.evaluate.return_value = False
        node.bounding_box.return_value = {'x': 704, 'y': 724, 'width': 32, 'height': 32}
        self.dispatcher._dom(context, mock.Mock(), 'o', 0, {'action': 'click'}, node)
        self.assertEqual(context.visual_location, self.location)
        wrapper = mock.Mock()
        wrapper.element_info.rectangle = SimpleNamespace(left=704, top=724, right=736, bottom=756)
        wrapper.get_toggle_state.return_value = False
        self.dispatcher._native_guard = mock.Mock()
        result = self.dispatcher._native(context, mock.Mock(), 'o', 0,
            {'action': 'check', 'checked': True}, SimpleNamespace(source='uia'), {'wrapper': wrapper})
        self.assertEqual(result, 'uia_toggle')
        self.assertEqual(context.visual_location, self.location)

    def test_semantic_dispatch_continues_when_optional_bounds_are_unavailable(self):
        context = mock.Mock(visual_location=None)
        node = mock.Mock()
        node.evaluate.return_value = False
        node.bounding_box.side_effect = RuntimeError('Provider has no bounds')
        self.assertEqual(self.dispatcher._dom(context, mock.Mock(), 'o', 0, {'action': 'click'}, node), 'dom_click')
        node.click.assert_called_once()
        self.assertIsNone(context.visual_location)
        wrapper = mock.Mock()
        type(wrapper).element_info = mock.PropertyMock(side_effect=RuntimeError('Provider has no bounds'))
        wrapper.get_toggle_state.return_value = False
        self.dispatcher._native_guard = mock.Mock()
        self.assertEqual(self.dispatcher._native(context, mock.Mock(), 'o', 0,
            {'action': 'check', 'checked': True}, SimpleNamespace(source='uia'), {'wrapper': wrapper}), 'uia_toggle')
        wrapper.toggle.assert_called_once()

    def test_batch_resets_location_between_actions(self):
        runtime = mock.Mock()
        target = SimpleNamespace(kind='desktop', target_id='target', metadata={})
        runtime.resolve_target.return_value = target
        runtime.observations.get.return_value = SimpleNamespace(metadata={})
        context = mock.Mock(owner='owner', visual_location=None)
        context.hold.side_effect = lambda _lock: nullcontext()
        dispatcher = ActionDispatcher(runtime)
        dispatcher._boundary = mock.Mock(return_value=('stable',))
        dispatcher._readback = mock.Mock(return_value={})

        def step(_context, _target, _oid, index, _step):
            self.assertIsNone(context.visual_location)
            if index == 0:
                context.visual_location = self.location
            return 'fixture'

        dispatcher._step = step
        dispatcher._batch(context, {'target_id': 'target', 'observation_id': 'o',
                                    'steps': [{'action': 'click'}, {'action': 'check'}]})
        self.assertIsNone(dispatcher._readback.call_args.kwargs['location'])
        self.assertEqual(dispatcher._readback.call_args.kwargs['step_index'], 1)


if __name__ == '__main__':
    unittest.main()
