"""One validated dispatch/readback path for individual and short batch actions."""
import hashlib
import math
from pathlib import Path
import time

from .operations import Cancelled
from .geometry import FrameGeometry
from .conditions import Conditions, TransientWatcher, validate_condition
from .targets import RuntimeFault


_ACTIONS = {'click', 'input', 'key', 'scroll', 'drag', 'select', 'check', 'hover', 'attach', 'close', 'clipboard', 'file', 'invoke', 'set_value', 'expand', 'window', 'dom_query', 'extract'}
_VISUAL_ACTIONS = {'click', 'invoke', 'input', 'check', 'hover', 'scroll', 'drag'}
_KEY_ALIASES = {'control': 'ctrl', 'esc': 'escape', 'meta': 'win', 'arrowleft': 'left',
                'arrowright': 'right', 'arrowup': 'up', 'arrowdown': 'down', 'return': 'enter'}


def _integer(value, minimum, maximum):
    return isinstance(value, int) and not isinstance(value, bool) and minimum <= value <= maximum


def _point(value):
    if not isinstance(value, dict):
        raise ValueError('Action target must be a reference or a frame point')
    if set(value) == {'ref'} and isinstance(value['ref'], str) and value['ref']:
        return
    if set(value) == {'frame_id', 'x', 'y'} and isinstance(value['frame_id'], str):
        if all(isinstance(value[key], (int, float)) and not isinstance(value[key], bool)
               and math.isfinite(value[key]) and value[key] >= 0 for key in ('x', 'y')):
            return
    raise ValueError('Use exactly ref, or frame_id and finite nonnegative x/y')


class ActionDispatcher:
    def __init__(self, runtime):
        self.runtime = runtime

    @staticmethod
    def validate(arguments):
        if not isinstance(arguments, dict) or set(arguments) - {'target_id', 'observation_id', 'steps', 'expect'}:
            raise ValueError('Unknown act arguments')
        for key in ('target_id', 'observation_id'):
            if not isinstance(arguments.get(key), str) or not arguments[key]:
                raise ValueError(key + ' is required')
        steps = arguments.get('steps')
        if not isinstance(steps, list) or not 1 <= len(steps) <= 8:
            raise ValueError('steps must contain 1 to 8 actions')
        fields = {'click': {'target', 'button', 'count'}, 'input': {'target', 'text', 'mode'},
                  'key': {'keys'}, 'scroll': {'target', 'direction', 'amount'},
                  'drag': {'from', 'to'}, 'select': {'target', 'values'},
                  'check': {'target', 'checked'}, 'hover': {'target'}, 'attach': {'target', 'files'}, 'close': set(),
                  'clipboard': {'op', 'text', 'path'}, 'file': {'op', 'path', 'text'},
                  'invoke': {'target'}, 'set_value': {'target', 'value'}, 'expand': {'target'},
                  'window': {'op', 'on'}, 'dom_query': {'selector', 'limit'}, 'extract': {'selector', 'limit'}}
        for step in steps:
            if not isinstance(step, dict) or step.get('action') not in _ACTIONS:
                raise ValueError('Unsupported action')
            action = step['action']
            if set(step) - fields[action] - {'action'}:
                raise ValueError('Unknown fields for ' + action)
            if action == 'window':
                if step.get('op') not in ('topmost', 'unbind', 'cleanup', 'highlight', 'activate'):
                    raise ValueError('window.op must be topmost, unbind, cleanup, highlight or activate')
                if 'on' in step and (step['op'] not in ('topmost', 'highlight') or not isinstance(step['on'], bool)):
                    raise ValueError('window.on must be boolean and is only for topmost or highlight')
            if action in ('dom_query', 'extract'):
                if not isinstance(step.get('selector'), str) or not step['selector'] or len(step['selector']) > 2000:
                    raise ValueError(action + '.selector must be a CSS selector up to 2000 characters')
                if not _integer(step.get('limit', 50), 1, 100):
                    raise ValueError(action + '.limit must be 1..100')
            if 'target' in fields[action]:
                _point(step.get('target'))
            if action == 'drag':
                _point(step.get('from'))
                _point(step.get('to'))
            if action == 'click' and (step.get('button', 'left') not in ('left', 'right') or not _integer(step.get('count', 1), 1, 2)):
                raise ValueError('click accepts left/right and count 1/2')
            if action == 'input' and (step.get('mode') not in ('replace', 'insert') or not isinstance(step.get('text'), str) or len(step['text']) > 12000):
                raise ValueError('input requires replace/insert and text up to 12000 characters')
            if action == 'check' and not isinstance(step.get('checked'), bool):
                raise ValueError('check requires a boolean checked value')
            if action == 'scroll' and (step.get('direction') not in ('up', 'down', 'left', 'right') or not _integer(step.get('amount', 3), 1, 100)):
                raise ValueError('scroll requires direction and amount 1..100')
            if action in ('invoke', 'set_value', 'expand') and set(step.get('target', {})) != {'ref'}:
                raise ValueError(action + ' requires a semantic ref target')
            if action == 'set_value' and (not isinstance(step.get('value'), str) or len(step['value']) > 12000):
                raise ValueError('set_value.value must be a string up to 12000 characters')
            if action == 'clipboard':
                op = step.get('op')
                if op not in ('get', 'set', 'paste_file'):
                    raise ValueError('clipboard.op must be get, set or paste_file')
                if op == 'set' and (not isinstance(step.get('text'), str) or len(step['text']) > 100000):
                    raise ValueError('clipboard.set requires text up to 100000 characters')
                if op == 'paste_file' and (not isinstance(step.get('path'), str) or not Path(step['path']).is_absolute() or not Path(step['path']).is_file()):
                    raise ValueError('clipboard.paste_file requires an existing absolute file path')
                if set(step) != {'action', 'op'} | ({'text'} if op == 'set' else {'path'} if op == 'paste_file' else set()):
                    raise ValueError('clipboard fields must match the selected op')
            if action == 'file':
                op = step.get('op')
                if op not in ('create', 'write', 'delete', 'list', 'read'):
                    raise ValueError('file.op must be create, write, delete, list or read')
                path = step.get('path', '')
                if not isinstance(path, str) or len(path) > 4096 or (not path and op != 'list'):
                    raise ValueError('file.path is required except for list')
                if op in ('create', 'write') and (not isinstance(step.get('text'), str) or len(step['text'].encode('utf-8')) > 2000000):
                    raise ValueError('file create/write requires text up to 2000000 UTF-8 bytes')
                if op not in ('create', 'write') and 'text' in step:
                    raise ValueError('file.text is only valid for create or write')
            list_key = {'key': 'keys', 'select': 'values', 'attach': 'files'}.get(action)
            if list_key:
                values = step.get(list_key)
                if not isinstance(values, list) or not 1 <= len(values) <= (8 if action == 'key' else 32) or any(not isinstance(value, str) or not value or len(value) > 4096 for value in values):
                    raise ValueError(list_key + ' requires a bounded nonempty string array')
                if action == 'attach' and any(not Path(value).is_absolute() or not Path(value).is_file() for value in values):
                    raise ValueError('Every attachment must be an existing absolute file path')
                if action == 'key':
                    from .game.input import pyautogui
                    if any(_KEY_ALIASES.get(value.lower(), value.lower()) not in pyautogui.KEYBOARD_KEYS for value in values):
                        raise ValueError('keys contains an unsupported key name. Provide each key separately, for example ["CTRL", "O"]')
        if any(step['action'] == 'window' and step['op'] in ('unbind', 'cleanup') for step in steps[:-1]):
            raise ValueError('window unbind/cleanup must be the final step')
        if 'expect' in arguments and steps[-1]['action'] == 'window' and steps[-1]['op'] in ('unbind', 'cleanup'):
            raise ValueError('window unbind/cleanup cannot have an expectation')
        if 'expect' in arguments:
            expect = arguments['expect']
            if not isinstance(expect, dict) or expect.get('kind') not in ('text', 'element', 'window', 'state'):
                raise ValueError('expect requires a supported condition')
            if not _integer(expect.get('after_step', len(steps) - 1), 0, len(steps) - 1):
                raise ValueError('expect.after_step is outside this batch')
            if not _integer(expect.get('timeout_ms', 5000), 100, 30000):
                raise ValueError('expect.timeout_ms must be 100..30000')
            if 'transient' in expect and not isinstance(expect['transient'], bool):
                raise ValueError('expect.transient must be boolean')
            if expect.get('transient') and expect['kind'] != 'text':
                raise ValueError('Transient expectations require a text condition')
            validate_condition({key: value for key, value in expect.items() if key not in ('after_step', 'timeout_ms', 'transient')})

    def run(self, context, arguments):
        target = self.runtime.resolve_target(context.owner, arguments['target_id'])
        call = self.runtime.browser.call if target.kind == 'browser' else self.runtime.observations.uia.call
        return context.backend(call, lambda: self._batch(context, arguments), timeout=120)

    def _batch(self, context, arguments):
        runtime, owner = self.runtime, context.owner
        target_id, oid = arguments['target_id'], arguments['observation_id']
        result = {'success': True, 'outcome': 'dispatched', 'observation_changed': None}
        target = runtime.resolve_target(owner, target_id)
        baseline = None
        try:
            baseline = runtime.observations.get(owner, oid, target_id)
            if baseline.metadata.get('document_id') and baseline.metadata['document_id'] != target.metadata.get('document_id'):
                raise RuntimeFault('REF_STALE', 'The action observation belongs to an earlier document')
        except RuntimeFault:
            context.change(result=self._readback(owner, target_id, baseline))
            raise
        with context.hold(runtime.operations.target_lock(target)):
            previous = self._boundary(owner, target_id)
            for index, step in enumerate(arguments['steps']):
                watcher = None
                watcher_slot = False
                before_observation = None
                context.visual_location = None
                before_local = None
                verification_error = None
                window_keys = None
                try:
                    expect = arguments.get('expect')
                    watch_step = (expect and expect.get('transient') and
                                  index == expect.get('after_step', len(arguments['steps']) - 1))
                    if watch_step:
                        while not runtime._transient_lock.acquire(timeout=0.05):
                            context.check()
                        watcher_slot = True
                    with context.hold(runtime._input_lock):
                        context.check()
                        target = runtime.resolve_target(owner, target_id)
                        if target.kind == 'window':
                            from .game.window import is_foreground_target
                            if not is_foreground_target(target.handle):
                                runtime.window.force_foreground(target.handle)
                        if watch_step:
                            watcher_slot = False
                            try:
                                watcher = TransientWatcher(runtime, context, target, expect, lock_held=True)
                            except Cancelled:
                                raise
                            except Exception as error:
                                verification_error = error
                            if watcher is not None:
                                def arm_dispatch(step_index):
                                    nonlocal verification_error
                                    try:
                                        watcher.dispatch(step_index)
                                    except Cancelled:
                                        raise
                                    except Exception as error:
                                        verification_error = error
                                context.before_dispatch = arm_dispatch
                        # Hover needs semantic elements as well as pixels. Fast frame captures
                        # intentionally contain no elements, so cannot establish its baseline.
                        if step['action'] == 'hover':
                            try:
                                before_observation = runtime.observations.observe(owner, target_id, image=True)
                            except Exception:
                                try:
                                    before_observation = runtime.observations.observe(owner, target_id, image=False)
                                except Exception:
                                    pass
                        elif step['action'] in _VISUAL_ACTIONS:
                            try:
                                frame = runtime.observations.capture_frame(owner, target_id)
                                before_observation = {'observation': {
                                    'observation_id': frame['observation_id'], 'frames': [frame]}}
                            except Exception:
                                pass
                        click_point = None
                        if step['action'] in ('click', 'invoke'):
                            try:
                                location = step['target']
                                if 'ref' in location:
                                    element, _ = runtime.observations.element(owner, oid, target_id, location['ref'])
                                    if element.rect is not None:
                                        x, y, width, height = element.rect
                                        click_point = (x + width / 2, y + height / 2)
                                else:
                                    click_point = runtime.map_point(owner, target_id, location['frame_id'], location['x'], location['y'])
                            except Exception:
                                pass
                        if target.kind == 'window' and click_point is not None:
                            try:
                                before_local = runtime.observations.capture_frame(owner, target_id, screen_point=click_point)
                            except Exception as error:
                                result['screen_region_warning'] = str(error)
                        if target.kind == 'window' and step['action'] in ('click', 'invoke'):
                            try:
                                from .desktop_tools import window_identity_snapshot
                                window_keys = {row['window_key'] for row in window_identity_snapshot().values()}
                            except Exception as error:
                                result['window_discovery_warning'] = str(error)
                        method = self._step(context, target, oid, index, step)
                        context.completed(index, method)
                        result['action'] = {'status': 'dispatched', 'step': index, 'method': method}
                        if step['action'] in ('click', 'invoke'):
                            try:
                                after_local = None
                                if before_local is not None:
                                    try:
                                        after_local = runtime.observations.capture_frame(owner, target_id, screen_point=click_point)
                                    except Exception as error:
                                        result['screen_region_warning'] = str(error)
                                result['post_click_frame'] = runtime.observations.capture_frame(owner, target_id)
                                evidence_before = ({'observation': {'frames': [before_local]}}
                                                   if after_local is not None else before_observation)
                                evidence_after = after_local or result['post_click_frame']
                                result.update(self._click_evidence(owner, target, click_point, evidence_before, evidence_after))
                                if watch_step and before_observation and result.get('local_sample_region'):
                                    try:
                                        result['popup_candidates'] = runtime.observations.popup_candidates(
                                            owner, evidence_before['observation']['frames'][0],
                                            evidence_after, result['local_sample_region'])
                                        result['popup_observation_id'] = evidence_after['observation_id']
                                    except Exception as error:
                                        result['popup_ocr_warning'] = str(error)
                            except Exception as error:
                                result['post_click_frame_warning'] = str(error)
                        time.sleep(0.03)
                        if window_keys is not None:
                            try:
                                # A launcher can create a child-process HWND after its
                                # menu disappears. Poll briefly without replaying input.
                                candidates = []
                                import win32gui
                                deadline = time.monotonic() + (0.5 if win32gui.IsWindow(target.handle) else 3.0)
                                while True:
                                    context.check()
                                    candidates = runtime.new_window_candidates(owner, target, window_keys)
                                    if candidates or time.monotonic() >= deadline:
                                        break
                                    context.cancelled.wait(0.05)
                                result['new_window_candidates'] = candidates
                                if candidates:
                                    result['next_action'] = 'Inspect new_window_candidates. Open the intended target_id or hwnd and obtain fresh observation refs before continuing.'
                            except Cancelled:
                                raise
                            except Exception as error:
                                result['window_discovery_warning'] = str(error)
                        try:
                            current = self._boundary(owner, target_id)
                        except RuntimeFault as error:
                            if error.code != 'TARGET_CLOSED':
                                raise
                            current = ('closed',)
                        if step['action'] == 'close' and current != ('closed',):
                            current = ('close_requested',)
                            result['close_pending'] = True
                        if result.get('new_window_candidates'):
                            current = ('new_windows', current, tuple(row['target_id'] for row in result['new_window_candidates']))
                        last = index == len(arguments['steps']) - 1
                        if last or current != previous:
                            if step['action'] == 'window' and step['op'] in ('unbind', 'cleanup'):
                                result.update(binding_released=True)
                            else:
                                result.update(self._readback(owner, target_id, baseline, before_observation=before_observation,
                                                             action=step.get('action'), location=context.visual_location, step_index=index, target=target,
                                                             candidates=result.get('new_window_candidates')))
                    expect = arguments.get('expect')
                    if expect and index == expect.get('after_step', len(arguments['steps']) - 1):
                        # Keep target order, release physical input while waiting.
                        try:
                            if verification_error is not None:
                                raise verification_error
                            result.update(watcher.wait() if watcher else self._expect(context, target_id, oid, expect))
                        except Cancelled:
                            raise
                        except Exception as error:
                            result['expectation'] = {'status': 'unknown', 'reason': str(error)}
                            if watcher is not None:
                                result['expectation'].update(timeline=list(watcher.timeline), dropped=watcher.dropped)
                            context.change(expectation=result['expectation'])
                        expectation_status = result['expectation']['status']
                        if watch_step and result.get('popup_candidates'):
                            candidates = result['popup_candidates']
                            needle = expect['text']
                            matching = [candidate for candidate in candidates
                                        if (candidate.get('raw_text') == needle if expect.get('match') == 'exact'
                                            else needle in candidate.get('raw_text', ''))]
                            if matching:
                                result['expectation'].setdefault('timeline', []).append({
                                    'source': 'popup_ocr', 'text': needle,
                                    'observation_id': result['popup_observation_id'],
                                    'refs': [candidate['ref'] for candidate in matching],
                                    'appearance_verified': False})
                                context.change(expectation=result['expectation'])
                        result['verification'] = {'status': 'unknown' if expectation_status in ('timeout', 'unknown') else expectation_status}
                        if result['expectation']['status'] != 'met':
                            if result.get('post_click_frame'):
                                next_action = ('Missing verification does not prove the action failed. Inspect post_click_frame and the local change region before clicking again. '
                                               'If needed, observe and relocate the control, or try keyboard navigation or physical coordinates.')
                            else:
                                next_action = 'Missing verification does not prove the action failed. Observe the target before deciding whether to retry.'
                            if result.get('toggle'):
                                next_action += ' The local appearance reverted after repeated clicks. Another click may open or close the same control.'
                            result.update(outcome='interrupted' if not last else 'dispatched',
                                          dispatch='partial' if not last else 'sent', stopped_at=index + 1,
                                          next_action=next_action)
                            break
                    if current != previous and not last:
                        result.update(outcome='interrupted', dispatch='partial', stopped_at=index + 1,
                                      reason='target_boundary_changed', next_action='observe')
                        break
                    previous = current
                except Cancelled:
                    result.update(self._readback(owner, target_id, baseline, before_observation=before_observation, action=step.get('action'), location=context.visual_location,
                                                 step_index=index, target=target, candidates=result.get('new_window_candidates')))
                    context.change(result=result)
                    raise
                except Exception as error:
                    result.update(self._readback(owner, target_id, baseline, before_observation=before_observation, action=step.get('action'), location=context.visual_location,
                                                 step_index=index, target=target, candidates=result.get('new_window_candidates')))
                    context.change(result=result, stopped_at=index)
                    raise
                finally:
                    context.before_dispatch = None
                    if watcher is not None:
                        watcher.close()
                    elif watcher_slot:
                        runtime._transient_lock.release()
        return result

    def _click_evidence(self, owner, target, point, before_observation, post_frame):
        """Compare a local area immediately after input without asserting business success."""
        import cv2
        import numpy as np

        if point is None:
            return {'popup_detected': None, 'local_visual_effect_reason': 'point_unavailable'}
        before_frames = (before_observation or {}).get('observation', {}).get('frames', [])
        if not before_frames:
            return {'popup_detected': None}
        # A click can advance the DOM revision. Historical evidence remains
        # comparable even though its coordinates are stale for another action.
        before_snapshot = self.runtime.observations.get(owner, before_frames[0]['observation_id'], target.target_id)
        after_snapshot = self.runtime.observations.get(owner, post_frame['observation_id'], target.target_id)
        before_frame = next(frame for frame in before_snapshot.frames if frame.frame_id == before_frames[0]['frame_id'])
        after_frame = next(frame for frame in after_snapshot.frames if frame.frame_id == post_frame['frame_id'])
        if before_frame.geometry != after_frame.geometry:
            return {'popup_detected': None, 'local_visual_effect_reason': 'geometry_changed'}
        before = cv2.imdecode(np.frombuffer(self.runtime.observations.image(owner, before_frame.frame_id), np.uint8), cv2.IMREAD_COLOR)
        after = cv2.imdecode(np.frombuffer(self.runtime.observations.image(owner, after_frame.frame_id), np.uint8), cv2.IMREAD_COLOR)
        if before is None or after is None or before.shape != after.shape:
            return {'popup_detected': None, 'local_visual_effect_reason': 'frame_unavailable'}
        x, y = after_frame.geometry.from_screen(*point)
        scale_x = after.shape[1] / after_frame.geometry.source_rect[2]
        scale_y = after.shape[0] / after_frame.geometry.source_rect[3]
        left, right = max(0, int(x - 200 * scale_x)), min(after.shape[1], int(x + 200 * scale_x))
        top, bottom = max(0, int(y - 200 * scale_y)), min(after.shape[0], int(y + 200 * scale_y))
        if right <= left or bottom <= top:
            return {'popup_detected': None, 'local_visual_effect_reason': 'point_outside_frame'}
        diff = cv2.cvtColor(cv2.absdiff(before[top:bottom, left:right], after[top:bottom, left:right]), cv2.COLOR_BGR2GRAY)
        mask = diff > 8
        ratio = float(np.count_nonzero(mask) / mask.size)
        changed_pixels = int(np.count_nonzero(mask))
        # The local threshold retains low-contrast changes without treating a
        # handful of JPEG edge pixels as reliable visual feedback.
        local_effect = ratio >= 0.005 and changed_pixels >= 16
        result = {'popup_detected': None, 'local_visual_effect': local_effect,
                  'local_visual_effect_changed_ratio': round(ratio, 6),
                  'local_visual_effect_threshold': {'pixel_delta': 8, 'changed_ratio': 0.005, 'min_pixels': 16},
                  'local_sample_region': {'frame_id': post_frame['frame_id'], 'x': left, 'y': top,
                                          'width': right - left, 'height': bottom - top},
                  'next_action': ('Inspect click_region_frames.before/after alongside post_click_frame and the control state before repeating the click. '
                                  'A later missing popup can be transient. Repeating a toggle can close it. '
                                  'If the expected state is absent, observe and relocate the control, or try keyboard navigation or physical coordinates.')}
        if np.any(mask):
            ys, xs = np.nonzero(mask)
            result['popup_region'] = {'frame_id': post_frame['frame_id'], 'x': left + int(xs.min()),
                                      'y': top + int(ys.min()), 'width': int(xs.max() - xs.min() + 1),
                                      'height': int(ys.max() - ys.min() + 1), 'kind': 'visual_change'}
        try:
            previous = self.runtime.observations.previous_click(owner, before_frames[0], post_frame, point)
            if previous is not None:
                prior_before, prior_after = [cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
                                             for data in previous]
                if (prior_before is not None and prior_after is not None
                        and prior_before.shape == prior_after.shape == before.shape):
                    def difference(first, second):
                        area = cv2.absdiff(first[top:bottom, left:right], second[top:bottom, left:right])
                        return float(np.count_nonzero(cv2.cvtColor(area, cv2.COLOR_BGR2GRAY) > 8) / mask.size)
                    continuity = difference(before, prior_after)
                    restored = difference(after, prior_before)
                    result['toggle_evidence'] = {'scope': 'local_pixels', 'continuity_changed_ratio': round(continuity, 6),
                                                 'restored_changed_ratio': round(restored, 6)}
                    if local_effect and continuity < 0.005 and restored < min(0.02, ratio * 0.2):
                        result['toggle'] = True
                        result['next_action'] = ('The local appearance returned to its state before the preceding click. '
                                                 'Another click may open or close the same control. Inspect the before/after images and control state first.')
        except Exception as error:
            result['toggle_warning'] = str(error)
        try:
            if after_frame.geometry.method == 'screen_region':
                result['click_region_frames'] = {'before': before_frames[0], 'after': post_frame}
            else:
                result['click_region_frames'] = {
                    'before': self.runtime.observations.crop_frame(owner, before_frames[0], result['local_sample_region']),
                    'after': self.runtime.observations.crop_frame(owner, post_frame, result['local_sample_region']),
                }
        except Exception as error:
            result['click_region_warning'] = str(error)
            result['next_action'] = 'Inspect post_click_frame and the observed control state before deciding whether to repeat the action.'
        return result

    def _readback(self, owner, target_id, baseline, before_observation=None, action=None, location=None, step_index=None, target=None, candidates=None):
        unavailable = {}
        if action in _VISUAL_ACTIONS:
            unavailable.update(visual_effect=None, visual_effect_basis='unknown',
                               visual_effect_step=step_index, visual_effect_reason='readback_failed')
        if action == 'hover':
            unavailable['hover_semantic_changes'] = {'status': 'unknown'}
        try:
            # Request image to ensure frames and bounds are populated in readback observation
            try:
                observed = self.runtime.observations.observe(owner, target_id, image=True)
            except Exception:
                if action != 'hover':
                    raise
                observed = self.runtime.observations.observe(owner, target_id, image=False)
            fresh = self.runtime.observations.get(owner, observed['observation']['observation_id'])
            changed = None if baseline is None else (fresh.text != baseline.text or
                [element.semantic for element in fresh.elements] != [element.semantic for element in baseline.elements])

            result = {'observation': observed['observation'], 'observation_changed': changed}
            if action == 'hover':
                try:
                    before_id = (before_observation or {}).get('observation', {}).get('observation_id')
                    before_semantic = self.runtime.observations.get(owner, before_id, target_id) if before_id else None
                    result['hover_semantic_changes'] = self._hover_semantic_changes(before_semantic, fresh)
                except Exception:
                    result['hover_semantic_changes'] = {'status': 'unknown'}
            if action in _VISUAL_ACTIONS:
                result.update(visual_effect=None, visual_effect_basis='unknown', visual_effect_step=step_index)
                before = (before_observation or {}).get('observation', {})
                after = observed['observation']
                before_frames, after_frames = before.get('frames', []), after.get('frames', [])
                evidence = {'before_observation_id': before.get('observation_id'),
                            'after_observation_id': after.get('observation_id'),
                            'before_frame_id': before_frames[0]['frame_id'] if before_frames else None,
                            'after_frame_id': after_frames[0]['frame_id'] if after_frames else None}
                result['visual_effect_frames'] = evidence
                try:
                    if before_frames and after_frames:
                        before_image = self.runtime.observations.image(owner, evidence['before_frame_id'])
                        after_image = self.runtime.observations.image(owner, evidence['after_frame_id'])
                        evidence.update(before_sha256=hashlib.sha256(before_image).hexdigest(),
                                        after_sha256=hashlib.sha256(after_image).hexdigest())
                        # Equal dimensions alone cannot establish the same coordinate frame.
                        geometry_keys = ('target_id', 'width', 'height', 'source_rect', 'crop', 'coordinate_origin', 'method',
                                         'dpi', 'window_rect', 'client_rect', 'virtual_origin')
                        if any(before_frames[0].get(key) != after_frames[0].get(key) for key in geometry_keys):
                            result['visual_effect_reason'] = 'geometry_mismatch'
                        else:
                            regions = self._visual_regions(before_frames[0], location)
                            result.update(self._compare_visual_effect(before_image, after_image, regions))
                    else:
                        result['visual_effect_reason'] = 'frame_missing' if before_observation else 'before_capture_unavailable'
                except RuntimeFault as error:
                    result['visual_effect_reason'] = 'frame_expired' if error.code == 'FRAME_EXPIRED' else 'frame_unavailable'
                except Exception:
                    result['visual_effect_reason'] = 'comparison_failed'
            return result
        except RuntimeFault as error:
            if error.code == 'TARGET_CLOSED':
                if action in _VISUAL_ACTIONS:
                    unavailable['visual_effect_reason'] = 'target_closed'
                if target is not None and target.kind == 'window' and action != 'close':
                    try:
                        replacement = self.runtime.rebind_window(owner, target, candidates=candidates)
                        if replacement is not None:
                            observed = self.runtime.observations.observe(owner, replacement.target_id, image=True)
                            observed['observation']['rebound_from'] = target_id
                            if action in _VISUAL_ACTIONS:
                                unavailable['visual_effect_reason'] = 'target_rebound'
                            return {**unavailable, 'closed': True, 'rebound': True, 'target_id': replacement.target_id,
                                    'observation': observed['observation'], 'observation_changed': True}
                    except Exception:
                        pass
                return {**unavailable,
                        'closed': True, 'observation_changed': True,
                        'next_action': 'Inspect new_window_candidates and refresh the target directory for delayed windows. Open the intended target and obtain fresh refs before continuing.'}
            return {**unavailable, 'readback_warning': str(error), 'observation_changed': None}
        except Exception as error:
            return {**unavailable, 'readback_warning': str(error), 'observation_changed': None}

    @staticmethod
    def _hover_semantic_changes(before, after):
        if before is None:
            return {'status': 'unknown'}
        if before.metadata.get('document_id') != after.metadata.get('document_id'):
            return {'status': 'unknown', 'reason': 'document_changed'}

        def keyed(observation):
            elements = {}
            incomplete = False
            for element in observation.elements:
                identity = None
                if element.source == 'dom':
                    identity = element.locator.get('token')
                elif element.source == 'uia':
                    identity = element.locator.get('runtime_id')
                if not identity:
                    incomplete = True
                    continue
                if isinstance(identity, list):
                    identity = tuple(identity)
                key = (element.source, element.target_id, element.document_id, identity)
                if key in elements:
                    elements[key] = None
                else:
                    elements[key] = element
            return elements, incomplete or any(element is None for element in elements.values())

        (previous, before_incomplete), (current, after_incomplete) = keyed(before), keyed(after)
        added, expanded = [], []
        for key, element in current.items():
            if element is None:
                continue
            old = previous.get(key)
            if key not in previous:
                added.append({'ref': element.ref, 'role': element.semantic.get('role'),
                              'name': str(element.semantic.get('name', ''))[:240]})
            elif old is not None and 'expanded' in old.semantic and 'expanded' in element.semantic:
                if old.semantic['expanded'] != element.semantic['expanded']:
                    expanded.append({'ref': element.ref, 'name': str(element.semantic.get('name', ''))[:240],
                                     'before': old.semantic['expanded'], 'after': element.semantic['expanded']})
        return {'status': 'observed', 'new_elements': added[:20], 'expanded_changes': expanded[:20],
                'truncated': len(added) > 20 or len(expanded) > 20 or
                             before_incomplete or after_incomplete or
                             bool(before.metadata.get('source_truncated') or after.metadata.get('source_truncated'))}

    @staticmethod
    def _visual_regions(frame, location):
        if not location or not frame.get('source_rect'):
            return []
        geometry = FrameGeometry(tuple(frame['source_rect']), (frame['width'], frame['height']),
                                 crop=tuple(frame['crop']) if frame.get('crop') else None)
        x, y = geometry.from_screen(*location['point'])
        if not 0 <= x < frame['width'] or not 0 <= y < frame['height']:
            return []
        regions = []
        if location.get('rect'):
            left, top, width, height = location['rect']
            left, top = geometry.from_screen(left, top)
            right, bottom = geometry.from_screen(location['rect'][0] + width, location['rect'][1] + height)
            regions.append({'basis': 'element_bbox', 'bbox': [left - 12, top - 12, right + 12, bottom + 12]})
        for basis, radius in (('click_neighborhood', 32), ('click_surroundings', 128)):
            regions.append({'basis': basis, 'bbox': [x - radius, y - radius, x + radius, y + radius]})
        return regions

    def _compare_visual_effect(self, before_image_bytes, after_image_bytes, regions=()):
        """Preserve global evidence and detect small responses around the action."""
        result = {'visual_effect': None, 'visual_effect_basis': 'unknown',
                  'visual_effect_thresholds': {'pixel_difference': 25, 'global_ratio': 0.005,
                                               'local_ratio': 0.01, 'local_min_pixels': 12},
                  'visual_effect_regions': []}
        try:
            # Vision packages are optional. Ordinary window/UIA actions must stay importable
            # when a machine has not provisioned the visual extra.
            import cv2
            import numpy as np

            # Decode JPEG images
            before_arr = np.frombuffer(before_image_bytes, np.uint8)
            after_arr = np.frombuffer(after_image_bytes, np.uint8)
            before = cv2.imdecode(before_arr, cv2.IMREAD_COLOR)
            after = cv2.imdecode(after_arr, cv2.IMREAD_COLOR)

            if before is None or after is None:
                return {**result, 'visual_effect_reason': 'decode_failed'}
            if before.shape != after.shape:
                return {**result, 'visual_effect_reason': 'shape_mismatch'}

            # Downscale for efficient comparison
            h, w = before.shape[:2]
            small_dim = (max(1, w // 2), max(1, h // 2))
            before_small = cv2.resize(before, small_dim, interpolation=cv2.INTER_AREA)
            after_small = cv2.resize(after, small_dim, interpolation=cv2.INTER_AREA)

            # Compute pixel differences
            diff = cv2.absdiff(before_small, after_small)
            gray = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)

            # Threshold to detect meaningful changes (threshold 25 same as watch.py)
            mask = (gray > 25).astype(np.uint8)
            changed_pixels = np.count_nonzero(mask)
            total_pixels = mask.size

            changed_ratio = changed_pixels / total_pixels
            global_changed = bool(changed_ratio > 0.005)
            for region in regions:
                left, top, right, bottom = region['bbox']
                left, top = max(0, math.floor(left)), max(0, math.floor(top))
                right, bottom = min(w, math.ceil(right)), min(h, math.ceil(bottom))
                if right <= left or bottom <= top:
                    continue
                # Retain fine icon edges already present in the captured frame.
                local_diff = cv2.absdiff(before[top:bottom, left:right], after[top:bottom, left:right])
                local_mask = cv2.cvtColor(local_diff, cv2.COLOR_BGR2GRAY) > 25
                pixels = int(np.count_nonzero(local_mask))
                ratio = pixels / local_mask.size
                result['visual_effect_regions'].append({
                    'basis': region['basis'], 'bbox': [left, top, right, bottom],
                    'changed_pixels': pixels, 'total_pixels': int(local_mask.size),
                    'changed_ratio': round(ratio, 6), 'changed': bool(ratio > 0.01 and pixels >= 12)})
            local_changed = any(region['changed'] for region in result['visual_effect_regions'])
            bases = ['full_frame'] if global_changed else []
            if local_changed:
                bases.append('local')
            basis = '_and_'.join(bases) or 'none'
            result.update(visual_effect=global_changed or local_changed, visual_effect_basis=basis,
                          visual_effect_changed_ratio=round(float(changed_ratio), 6),
                          visual_effect_global_changed=global_changed,
                          visual_effect_local_changed=local_changed if result['visual_effect_regions'] else None)
            near_changed = any(region['changed'] for region in result['visual_effect_regions']
                               if region['basis'] in ('element_bbox', 'click_neighborhood'))
            if result['visual_effect'] and not near_changed:
                surroundings_changed = any(region['changed'] for region in result['visual_effect_regions']
                                           if region['basis'] == 'click_surroundings')
                result.update(visual_effect_attribution='unknown',
                              visual_effect_attribution_reason='surroundings_only' if surroundings_changed else 'full_frame_only',
                              visual_effect_warning='The relationship between the visual change and the action is unknown. Verify the expected UI state.')
            return result
        except Exception:
            # Missing or unreadable vision inputs should not be reported as a verified no-op.
            return {**result, 'visual_effect_reason': 'comparison_unavailable'}

    def _boundary(self, owner, target_id):
        target = self.runtime.resolve_target(owner, target_id)
        if target.kind == 'browser':
            page, frame = target.handle
            dialog = self.runtime.browser.dialog_metadata(page)
            if dialog:
                return 'browser_dialog', dialog['type'], dialog['message']
            dialogs = frame.evaluate('''() => Array.from(document.querySelectorAll('dialog[open],[role=dialog],[aria-modal=true]'))
              .filter(el => el.getClientRects().length).map(el => el.textContent).join('|')''')
            return (target.metadata['document_id'], tuple(row[0] for row in target.metadata.get('descendants', [])),
                    tuple(id(page) for page in page.context.pages), dialogs)
        if target.kind == 'window':
            import win32gui
            from .desktop_tools import window_identity_snapshot
            owned = tuple(sorted(row['hwnd'] for row in window_identity_snapshot().values()
                                 if row['owner_hwnd'] == target.handle or
                                 row.get('pid') == target.metadata.get('pid') and row['hwnd'] != target.handle))
            return target.identity, owned, bool(win32gui.IsWindowEnabled(target.handle))
        return target.identity,

    def _resolve(self, context, target, oid, location):
        if 'ref' in location:
            element, live_target = self.runtime.observations.element(context.owner, oid, target.target_id, location['ref'])
            if element.source == 'dom':
                return element, self.runtime.observations.dom_element(context.owner, element)
            if element.source == 'uia':
                return element, self.runtime.observations.validate_uia(context.owner, element, action='identity')
            frame = self.runtime.frames.get(context.owner, element.locator['frame_id'], live_target)
            if frame.target_id != target.target_id:
                raise RuntimeFault('TARGET_MISMATCH', 'Visual reference belongs to another frame')
            x, y, width, height = element.rect
            return element, (x + width / 2, y + height / 2)
        observation = self.runtime.observations.get(context.owner, oid, target.target_id)
        valid_frames = [frame.frame_id for frame in observation.frames]
        if location['frame_id'] not in valid_frames:
            valid_frames_str = ', '.join(valid_frames) if valid_frames else '(none)'
            raise RuntimeFault('FRAME_MISMATCH',
                             f"Frame point must belong to the action observation. Requested: {location['frame_id']}, valid: {valid_frames_str}")
        return None, self.runtime.map_point(context.owner, target.target_id, location['frame_id'], location['x'], location['y'])

    def _step(self, context, target, oid, index, step):
        action = step['action']
        if action == 'window':
            context.dispatch(index)
            return self.runtime.window_action(context.owner, target, step, context.operation_id)
        if action in ('dom_query', 'extract'):
            if target.kind != 'browser':
                raise RuntimeFault('TARGET_KIND', action + ' requires a browser target')
            context.check()
            return self.runtime.browser.query(context.owner, target.target_id, step)
        if action in ('key', 'close', 'clipboard', 'file'):
            return self._global_step(context, target, index, step)
        element = node = None
        try:
            element, node = self._resolve(context, target, oid, step.get('target', step.get('from')))
            if element and element.source == 'dom':
                return self._dom(context, target, oid, index, step, node)
            return self._native(context, target, oid, index, step, element, node)
        finally:
            if element and element.source == 'dom' and node is not None:
                node.dispose()

    def _dom(self, context, target, oid, index, step, node):
        action = step['action']
        if action in ('invoke', 'set_value', 'expand'):
            raise RuntimeFault('UNSUPPORTED_ACTION', 'This action requires a UIA reference')
        blocked = node.evaluate('''el => {
          const dialogs = Array.from(el.ownerDocument.querySelectorAll('dialog[open],[aria-modal=true]'))
            .filter(dialog => dialog.getClientRects().length);
          return dialogs.length > 0 && !dialogs[dialogs.length - 1].contains(el);
        }''')
        if blocked:
            raise RuntimeFault('MODAL_OPEN', 'A modal dialog covers the observed element')
        if action == 'check' and node.is_checked() == step['checked']:
            return 'already_satisfied'
        if not node.is_enabled():
            raise RuntimeFault('ELEMENT_DISABLED', 'Observed element is disabled')
        if action == 'drag':
            other_element, other = self._resolve(context, target, oid, step['to'])
            try:
                start = node.bounding_box()
                end = other.bounding_box() if other_element and other_element.source == 'dom' else None
                if not start:
                    raise RuntimeFault('REF_STALE', 'Drag source has no live bounds')
                origin = (start['x'] + start['width'] / 2, start['y'] + start['height'] / 2)
                destination = (end['x'] + end['width'] / 2, end['y'] + end['height'] / 2) if end else other
                self._browser_pointer(context, target, index, step, origin, destination)
            finally:
                if other_element and other_element.source == 'dom':
                    other.dispose()
            return 'dom_drag'
        if action == 'select':
            available = node.evaluate('el => ({multiple: !!el.multiple, labels: Array.from(el.options || []).map(option => option.label)})')
            if not available['multiple'] and len(step['values']) != 1:
                raise RuntimeFault('SELECTION_MISMATCH', 'A single-select control accepts one exact label')
            if any(available['labels'].count(value) != 1 for value in step['values']):
                raise RuntimeFault('SELECTION_MISMATCH', 'Each requested option label must identify exactly one option')
        if action in ('click', 'input', 'check', 'hover', 'scroll'):
            try:
                box = node.bounding_box()
                if box:
                    context.visual_location = {
                        'point': (box['x'] + box['width'] / 2, box['y'] + box['height'] / 2),
                        'rect': (box['x'], box['y'], box['width'], box['height'])}
            except Exception:
                # Optional visual evidence must not prevent semantic input dispatch.
                pass
        context.dispatch(index)
        if action == 'click':
            try:
                node.click(button=step.get('button', 'left'), click_count=step.get('count', 1), timeout=3000, no_wait_after=True)
            except Exception:
                if self.runtime.browser.dialog_metadata(target.handle[0]) is None:
                    raise
                return 'dom_click_dialog'
        elif action == 'input':
            if step['mode'] == 'replace':
                node.fill(step['text'], timeout=3000)
            else:
                node.focus()
                context.guard_input()
                target.handle[0].keyboard.insert_text(step['text'])
        elif action == 'check':
            node.set_checked(step['checked'], timeout=3000)
        elif action == 'select':
            selected = node.select_option(label=step['values'], timeout=3000)
            if len(selected) != len(step['values']):
                raise RuntimeFault('SELECTION_MISMATCH', 'Application did not select all requested options')
        elif action == 'attach':
            if node.evaluate("el => el.tagName === 'INPUT' && el.type === 'file'"):
                node.set_input_files(step['files'], timeout=3000)
            else:
                with target.handle[0].expect_file_chooser(timeout=3000) as chooser:
                    node.click(timeout=3000, no_wait_after=True)
                context.guard_input()
                chooser.value.set_files(step['files'], timeout=3000)
        elif action == 'hover':
            node.hover(timeout=3000)
        elif action == 'scroll':
            node.hover(timeout=3000)
            context.guard_input()
            self._wheel(target, step)
        return 'dom_' + action

    def _wheel(self, target, step):
        amount = step.get('amount', 3) * 120
        direction = step['direction']
        target.handle[0].mouse.wheel(amount * (-1 if direction == 'left' else 1) if direction in ('left', 'right') else 0,
                                    amount * (-1 if direction == 'up' else 1) if direction in ('up', 'down') else 0)

    def _browser_pointer(self, context, target, index, step, point, destination=None):
        page = target.handle[0]
        if step['action'] not in ('click', 'hover', 'scroll', 'drag', 'input'):
            raise RuntimeFault('UNSUPPORTED_ACTION', 'This action requires a semantic element reference')
        if step['action'] == 'drag':
            context.visual_location = {'point': destination}
        elif context.visual_location is None:
            context.visual_location = {'point': point}
        context.dispatch(index)
        page.mouse.move(*point)
        context.guard_input()
        action = step['action']
        if action == 'click':
            page.mouse.click(*point, button=step.get('button', 'left'), click_count=step.get('count', 1))
        elif action == 'scroll':
            self._wheel(target, step)
        elif action == 'input':
            page.mouse.click(*point)
            context.guard_input()
            if step['mode'] == 'replace':
                page.keyboard.press('Control+A')
                context.guard_input()
                page.keyboard.press('Backspace')
            context.guard_input()
            page.keyboard.insert_text(step['text'])
        elif action == 'drag':
            page.mouse.down()
            try:
                for part in range(1, 9):
                    context.guard_input()
                    page.mouse.move(*(point[axis] + (destination[axis] - point[axis]) * part / 8 for axis in (0, 1)))
            finally:
                page.mouse.up()

    def _native_guard(self, context, target, geometry=None):
        context.guard_input()
        live = self.runtime.resolve_target(context.owner, target.target_id)
        if geometry is not None and live.metadata.get('window_rect') != geometry:
            raise RuntimeFault('TARGET_CHANGED', 'Window geometry changed before physical input')
        if target.kind == 'window':
            from .desktop_tools import _require_foreground
            refused = _require_foreground(target.handle, strict=True)
            if refused:
                import win32gui
                foreground = win32gui.GetForegroundWindow()
                raise RuntimeFault('FOCUS_LOST', f'Target {target.handle} did not receive foreground focus. '
                                   f'Current window {foreground}: {win32gui.GetWindowText(foreground)!r}')
            import win32gui
            if not win32gui.IsWindowEnabled(target.handle):
                raise RuntimeFault('MODAL_OPEN', 'Target is disabled by a modal window')

    def _native(self, context, target, oid, index, step, element, node):
        action = step['action']
        if element and element.source == 'uia':
            wrapper = node['wrapper']
            if not wrapper.is_enabled():
                raise RuntimeFault('ELEMENT_DISABLED', 'Observed control is disabled')
            self._native_guard(context, target)
            if action in ('click', 'invoke', 'input', 'check', 'hover', 'scroll'):
                try:
                    rect = wrapper.element_info.rectangle
                    context.visual_location = {
                        'point': ((rect.left + rect.right) / 2, (rect.top + rect.bottom) / 2),
                        'rect': (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)}
                except Exception:
                    # Some semantic-only providers expose no usable rectangle.
                    pass
            if action in ('invoke', 'set_value', 'expand'):
                try:
                    pattern = {'invoke': 'iface_invoke', 'set_value': 'iface_value', 'expand': 'iface_expand_collapse'}[action]
                    interface = getattr(wrapper, pattern)
                except Exception as error:
                    raise RuntimeFault('UNSUPPORTED_ACTION', f'UIA {action} pattern is unavailable: {error}') from error
                context.dispatch(index)
                if action == 'invoke':
                    interface.Invoke()
                elif action == 'set_value':
                    interface.SetValue(step['value'])
                else:
                    interface.Expand()
                return 'uia_' + action
            if action == 'check':
                if bool(wrapper.get_toggle_state()) == step['checked']:
                    return 'already_satisfied'
                context.dispatch(index)
                wrapper.toggle()
                return 'uia_toggle'
            if action == 'select':
                if len(step['values']) != 1:
                    raise RuntimeFault('UNSUPPORTED_ACTION', 'This native selection accepts one exact label')
                context.dispatch(index)
                wrapper.select(step['values'][0])
                return 'uia_select'
            if action == 'attach':
                raise RuntimeFault('UNSUPPORTED_ACTION', 'Choose a file dialog filename field using input and key, or a DOM file input')
            rect = wrapper.element_info.rectangle
            point = ((rect.left + rect.right) / 2, (rect.top + rect.bottom) / 2)
        else:
            point = node
        if action in ('click', 'input', 'hover', 'scroll') and context.visual_location is None:
            context.visual_location = {'point': point}
        destination = None
        if action == 'drag':
            other_element, other = self._resolve(context, target, oid, step['to'])
            try:
                if other_element and other_element.source == 'uia':
                    rect = other['wrapper'].element_info.rectangle
                    destination = ((rect.left + rect.right) / 2, (rect.top + rect.bottom) / 2)
                elif other_element and other_element.source == 'dom':
                    rect = other.bounding_box()
                    destination = (rect['x'] + rect['width'] / 2, rect['y'] + rect['height'] / 2)
                else:
                    destination = other
            finally:
                if other_element and other_element.source == 'dom':
                    other.dispose()
        if action == 'drag':
            context.visual_location = {'point': destination}
        if target.kind == 'browser':
            self._browser_pointer(context, target, index, step, point, destination)
            return 'browser_point_' + action
        if action not in ('click', 'hover', 'scroll', 'drag', 'input'):
            raise RuntimeFault('UNSUPPORTED_ACTION', 'This action requires a supported semantic reference')
        if action == 'input' and element and element.source == 'uia' and not element.semantic.get('editable'):
            raise RuntimeFault('UNSUPPORTED_ACTION', 'The observed native control is not editable')
        geometry = target.metadata.get('window_rect')
        def guard():
            self._native_guard(context, target, geometry)
            self._verify_hit(target, point)
        self._verify_hit(target, point)
        if destination is not None:
            self._verify_hit(target, destination)
        with self.runtime.input.dispatch_context(guard):
            guard()
            context.dispatch(index)
            sent = None
            if action == 'click':
                if step.get('button') == 'right':
                    for _ in range(step.get('count', 1)):
                        sent = self.runtime.input.right_click_screen(*point)
                        self._sent(sent)
                else:
                    sent = self.runtime.input.click_screen(*point, clicks=step.get('count', 1))
            elif action == 'hover':
                sent = self.runtime.input.mouse_move_screen(*point)
            elif action == 'scroll':
                direction = step['direction']
                sent = self.runtime.input.scroll(*point, step.get('amount', 3) * (-1 if direction in ('down', 'left') else 1),
                                                 direction='horizontal' if direction in ('left', 'right') else 'vertical')
            elif action == 'drag':
                sent = self.runtime.input.swipe_screen(point, destination, speed=8)
            elif action == 'input':
                if element and element.source == 'uia':
                    node['wrapper'].set_focus()
                    if not node['wrapper'].has_keyboard_focus():
                        raise RuntimeFault('FOCUS_LOST', 'Observed edit control did not receive keyboard focus')
                else:
                    self._sent(self.runtime.input.click_screen(*point))
                guard()
                if step['mode'] == 'replace':
                    self._sent(self.runtime.input.hotkey('ctrl', 'a'))
                    self._sent(self.runtime.input.press_key('backspace', wait_time=0))
                if step['text']:
                    sent = self.runtime.input.type_text(step['text'])
            self._sent(sent)
        return 'physical_' + action

    @staticmethod
    def _sent(value):
        if isinstance(value, dict) and value.get('success') is False:
            raise RuntimeFault('INPUT_REFUSED', str(value.get('error')))

    @staticmethod
    def _verify_hit(target, point):
        if target.kind != 'window':
            return
        import win32gui
        hwnd = win32gui.WindowFromPoint(tuple(round(value) for value in point))
        root = win32gui.GetAncestor(hwnd, 2)
        from .game.window import _has_owner_link
        if root != target.handle and not _has_owner_link(root, target.handle):
            raise RuntimeFault('TARGET_OCCLUDED', 'Physical point is covered by another window')

    def _global_step(self, context, target, index, step):
        if step['action'] == 'file':
            root = (self.runtime.home / 'desktop_workspace').resolve()
            path = (root / step.get('path', '')).resolve()
            if not path.is_relative_to(root) or (path == root and step['op'] != 'list'):
                raise RuntimeFault('PATH_DENIED', 'File path must be inside the desktop workspace')
            context.dispatch(index)
            op = step['op']
            if op == 'list':
                if not path.exists() and path == root:
                    root.mkdir(parents=True, exist_ok=True)
                entries = [{'name': item.name, 'directory': item.is_dir()} for item in sorted(path.iterdir())]
                return {'action': 'file', 'op': op, 'path': str(path), 'entries': entries}
            if op == 'create':
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open('x', encoding='utf-8', newline='') as stream:
                    stream.write(step['text'])
            elif op == 'write':
                with path.open('r+', encoding='utf-8', newline='') as stream:
                    stream.seek(0)
                    stream.write(step['text'])
                    stream.truncate()
            elif op == 'delete':
                path.unlink()
            else:
                if path.stat().st_size > 2000000:
                    raise RuntimeFault('FILE_TOO_LARGE', 'File exceeds 2000000 bytes')
                with path.open('r', encoding='utf-8', newline='') as stream:
                    text = stream.read(2000001)
                if len(text.encode('utf-8')) > 2000000:
                    raise RuntimeFault('FILE_TOO_LARGE', 'File exceeds 2000000 bytes')
                return {'action': 'file', 'op': op, 'path': str(path), 'text': text}
            return {'action': 'file', 'op': op, 'path': str(path)}
        if step['action'] == 'clipboard':
            import struct
            import win32clipboard

            op = step['op']
            if op == 'paste_file':
                if target.kind != 'window':
                    raise RuntimeFault('UNSUPPORTED_ACTION', 'File paste requires a selected native window')
                self._native_guard(context, target)
            context.dispatch(index)
            win32clipboard.OpenClipboard()
            try:
                if op == 'get':
                    text = (win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
                            if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT) else None)
                    return {'action': 'clipboard', 'op': op, 'text': text}
                win32clipboard.EmptyClipboard()
                if op == 'set':
                    win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, step['text'])
                else:
                    payload = struct.pack('<IiiII', 20, 0, 0, 0, 1)
                    payload += (str(Path(step['path']).resolve()) + '\0\0').encode('utf-16le')
                    win32clipboard.SetClipboardData(win32clipboard.CF_HDROP, payload)
                    drop_effect = win32clipboard.RegisterClipboardFormat('Preferred DropEffect')
                    win32clipboard.SetClipboardData(drop_effect, struct.pack('<I', 1))
            finally:
                win32clipboard.CloseClipboard()
            if op == 'paste_file':
                with self.runtime.input.dispatch_context(lambda: self._native_guard(context, target)):
                    self._native_guard(context, target)
                    self._sent(self.runtime.input.hotkey('ctrl', 'v'))
            return {'action': 'clipboard', 'op': op}
        if step['action'] == 'close':
            if target.kind == 'desktop':
                raise RuntimeFault('UNSUPPORTED_ACTION', 'Select a window or tab to close')
            if target.kind == 'browser' and target.handle[1] is not target.handle[0].main_frame:
                raise RuntimeFault('UNSUPPORTED_ACTION', 'Select the containing tab to close an iframe')
            context.dispatch(index)
            if target.kind == 'browser':
                self.runtime.browser.close_target(context.owner, target.target_id, respect_before_unload=True)
            else:
                import win32con
                import win32gui
                win32gui.PostMessage(target.handle, win32con.WM_CLOSE, 0, 0)
            return 'close_request'
        keys = [_KEY_ALIASES.get(key.lower(), key.lower()) for key in step['keys']]
        if target.kind == 'browser':
            dialog = self.runtime.browser.pending_dialogs.get(target.handle[0])
            if dialog is not None:
                if len(keys) != 1 or keys[0].upper() not in ('ENTER', 'ESC', 'ESCAPE'):
                    raise RuntimeFault('MODAL_OPEN', 'Use Enter to accept or Escape to dismiss the current browser dialog')
                context.dispatch(index)
                if keys[0].upper() == 'ENTER':
                    dialog.accept()
                else:
                    dialog.dismiss()
                self.runtime.browser.pending_dialogs.pop(target.handle[0], None)
                return 'dom_dialog_key'
            aliases = {'CTRL': 'Control', 'ALT': 'Alt', 'SHIFT': 'Shift', 'WIN': 'Meta', 'ENTER': 'Enter',
                       'ESC': 'Escape', 'ESCAPE': 'Escape', 'TAB': 'Tab', 'BACKSPACE': 'Backspace',
                       'DELETE': 'Delete', 'UP': 'ArrowUp', 'DOWN': 'ArrowDown', 'LEFT': 'ArrowLeft', 'RIGHT': 'ArrowRight'}
            aliases.update({'SPACE': 'Space', 'HOME': 'Home', 'END': 'End', 'PAGEUP': 'PageUp', 'PAGEDOWN': 'PageDown',
                            'INSERT': 'Insert', 'CAPSLOCK': 'CapsLock', 'PRINTSCREEN': 'PrintScreen', 'PAUSE': 'Pause',
                            'NUMLOCK': 'NumLock', 'SCROLLLOCK': 'ScrollLock', '+': 'Plus'})
            target.handle[0].bring_to_front()
            target.handle[1].evaluate('window.focus()')
            context.dispatch(index)
            target.handle[0].keyboard.press('+'.join(aliases.get(key.upper(), key.upper() if key.startswith('f') and key[1:].isdigit() else key) for key in keys))
            return 'dom_key'
        if target.kind != 'window':
            raise RuntimeFault('UNSUPPORTED_ACTION', 'Keyboard input requires a selected window or tab')
        with self.runtime.input.dispatch_context(lambda: self._native_guard(context, target)):
            self._native_guard(context, target)
            context.dispatch(index)
            self._sent(self.runtime.input.hotkey(*(key.lower() for key in keys)))
        return 'physical_key'

    def _expect(self, context, target_id, oid, condition):
        return Conditions(self.runtime).wait(context, target_id, oid, condition, condition.get('timeout_ms', 5000))
