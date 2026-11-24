"""One validated dispatch/readback path for individual and short batch actions."""
import math
from pathlib import Path
import time

from .operations import Cancelled
from .conditions import Conditions, TransientWatcher, validate_condition
from .targets import RuntimeFault


_ACTIONS = {'click', 'input', 'key', 'scroll', 'drag', 'select', 'check', 'hover', 'attach', 'close'}
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
                  'check': {'target', 'checked'}, 'hover': {'target'}, 'attach': {'target', 'files'}, 'close': set()}
        for step in steps:
            if not isinstance(step, dict) or step.get('action') not in _ACTIONS:
                raise ValueError('Unsupported action')
            action = step['action']
            if set(step) - fields[action] - {'action'}:
                raise ValueError('Unknown fields for ' + action)
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
                        raise ValueError('keys contains an unsupported key name')
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
                            watcher = TransientWatcher(runtime, context, target, expect, lock_held=True)
                            context.before_dispatch = watcher.dispatch
                        method = self._step(context, target, oid, index, step)
                        context.completed(index, method)
                        time.sleep(0.03)
                        try:
                            current = self._boundary(owner, target_id)
                        except RuntimeFault as error:
                            if error.code != 'TARGET_CLOSED':
                                raise
                            current = ('closed',)
                        if step['action'] == 'close' and current != ('closed',):
                            current = ('close_requested',)
                            result['close_pending'] = True
                        last = index == len(arguments['steps']) - 1
                        if last or current != previous:
                            result.update(self._readback(owner, target_id, baseline))
                    expect = arguments.get('expect')
                    if expect and index == expect.get('after_step', len(arguments['steps']) - 1):
                        # Keep target order, release physical input while waiting.
                        result.update(watcher.wait() if watcher else self._expect(context, target_id, oid, expect))
                        if result['expectation']['status'] != 'met':
                            result.update(success=False, outcome='interrupted', dispatch='partial' if not last else 'sent',
                                          stopped_at=index + 1, error={'code': 'EXPECTATION_TIMEOUT', 'message': 'Condition did not become true'})
                            break
                    if current != previous and not last:
                        result.update(outcome='interrupted', dispatch='partial', stopped_at=index + 1,
                                      reason='target_boundary_changed', next_action='observe')
                        break
                    previous = current
                except Cancelled:
                    result.update(self._readback(owner, target_id, baseline))
                    context.change(result=result)
                    raise
                except Exception as error:
                    result.update(self._readback(owner, target_id, baseline))
                    context.change(result=result, stopped_at=index)
                    raise
                finally:
                    context.before_dispatch = None
                    if watcher is not None:
                        watcher.close()
                    elif watcher_slot:
                        runtime._transient_lock.release()
        return result

    def _readback(self, owner, target_id, baseline):
        try:
            observed = self.runtime.observations.observe(owner, target_id)
            fresh = self.runtime.observations.get(owner, observed['observation']['observation_id'])
            changed = None if baseline is None else (fresh.text != baseline.text or
                [element.semantic for element in fresh.elements] != [element.semantic for element in baseline.elements])
            return {'observation': observed['observation'], 'observation_changed': changed}
        except RuntimeFault as error:
            if error.code == 'TARGET_CLOSED':
                return {'closed': True, 'observation_changed': True}
            return {'readback_warning': str(error), 'observation_changed': None}
        except Exception as error:
            return {'readback_warning': str(error), 'observation_changed': None}

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
            from .desktop_tools import _list_visible_windows
            owned = tuple(sorted(row['hwnd'] for row in _list_visible_windows()
                                 if win32gui.GetWindow(row['hwnd'], 4) == target.handle or
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
        if location['frame_id'] not in [frame.frame_id for frame in observation.frames]:
            raise RuntimeFault('FRAME_MISMATCH', 'Frame point must belong to the action observation')
        return None, self.runtime.map_point(context.owner, target.target_id, location['frame_id'], location['x'], location['y'])

    def _step(self, context, target, oid, index, step):
        action = step['action']
        if action in ('key', 'close'):
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
            if action == 'check':
                if bool(wrapper.get_toggle_state()) == step['checked']:
                    return 'already_satisfied'
                context.dispatch(index)
                wrapper.toggle()
                return 'uia_toggle'
            if action == 'input' and step['mode'] == 'replace':
                if not element.semantic.get('editable'):
                    raise RuntimeFault('UNSUPPORTED_ACTION', 'The observed native control is not editable')
                from pywinauto.uia_defines import NoPatternInterfaceError
                try:
                    pattern = wrapper.iface_value
                except NoPatternInterfaceError:
                    pattern = None
                if pattern is not None:
                    context.dispatch(index)
                    pattern.SetValue(step['text'])
                    return 'uia_value'
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
        if root != target.handle:
            raise RuntimeFault('TARGET_OCCLUDED', 'Physical point is covered by another window')

    def _global_step(self, context, target, index, step):
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
