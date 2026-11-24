"""Finite, observable conditions shared by action expectations and waits."""
import threading
import time
import uuid

from .observations import timestamp

from .targets import RuntimeFault


_ARM_BROWSER = r'''({key, text, exact, interval, timeout}) => {
  const matches = value => exact ? value === text : value.includes(text);
  const body = () => document.body?.innerText || '';
  const state = {baseline_present: matches(body()), seen_absent: !matches(body()),
    armed: false, matched_at: null, samples: 0, dispatched_at: null};
  const sample = records => {
    const current = body();
    state.samples++;
    if (!state.armed || state.matched_at || performance.now() > state.deadline) return;
    if (!matches(current)) state.seen_absent = true;
    let found = matches(current);
    if (!exact && records) found ||= records.some(record => Array.from(record.addedNodes).some(node => {
      const element = node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement;
      return element?.isConnected && element.getClientRects().length && matches(element.innerText || '');
    }));
    if (state.seen_absent && found) state.matched_at = new Date().toISOString();
  };
  const observer = new MutationObserver(sample);
  observer.observe(document, {subtree: true, childList: true, characterData: true});
  const timer = setInterval(() => sample(), interval);
  state.dispatch = () => {
    observer.takeRecords();
    state.seen_absent = !matches(body());
    state.dispatched_at = new Date().toISOString();
    state.deadline = performance.now() + timeout;
    state.armed = true;
  };
  state.close = () => { clearInterval(timer); observer.disconnect(); delete window[key]; };
  window[key] = state;
  return {baseline_present: state.baseline_present};
}'''


class TransientWatcher:
    """Collect text evidence from before dispatch until match, cancellation or timeout."""
    def __init__(self, runtime, context, target, condition, *, lock_held=False):
        self.runtime, self.context, self.target = runtime, context, target
        self.condition = condition
        self.timeout = condition.get('timeout_ms', 5000) / 1000
        self.stopped = threading.Event()
        self.dispatched = threading.Event()
        self.thread = None
        self.error = None
        self.result = None
        self.samples = 1
        self.started = None
        self.key = '__dsh_watch_' + uuid.uuid4().hex
        if not lock_held:
            while not runtime._transient_lock.acquire(timeout=0.05):
                context.check()
        try:
            if target.kind == 'browser':
                self.frame = target.handle[1]
                baseline = self.frame.evaluate(_ARM_BROWSER, {'key': self.key, 'text': condition['text'],
                    'exact': condition.get('match') == 'exact', 'interval': 100, 'timeout': self.timeout * 1000})
                self.baseline = baseline['baseline_present']
            elif target.kind == 'window':
                self.baseline = self._sample_native()
                self.thread = threading.Thread(target=self._poll_native, name='dsh-transient-watch', daemon=True)
                self.thread.start()
            else:
                raise RuntimeFault('UNSUPPORTED_CONDITION', 'Transient text requires a window or browser target')
            context.change(expectation={'status': 'pending', 'baseline_present': self.baseline, 'samples': 1})
        except BaseException:
            self.stopped.set()
            if target.kind == 'browser' and hasattr(self, 'frame'):
                try:
                    self.frame.evaluate('key => window[key]?.close()', self.key)
                except Exception:
                    pass
            if self.thread is not None:
                self.thread.join(timeout=3.1)
            runtime._transient_lock.release()
            raise

    def _sample_native(self):
        def sample():
            from pywinauto import Desktop
            from .desktop_tools import _uia_batch_elements
            target = self.runtime.resolve_target(self.context.owner, self.target.target_id)
            root = Desktop(backend='uia').window(handle=target.handle).wrapper_object()
            region = self.runtime.window.get_client_region(target.handle)
            if region is None:
                raise RuntimeFault('TARGET_CLOSED', 'Target client area is unavailable')
            nodes = _uia_batch_elements(root, region, 12, 1500, time.monotonic() + 2, semantics=True)
            text = '\n'.join(str(node.get('value') or node['name']) for node in nodes)[:60000]
            needle = self.condition['text']
            return text == needle if self.condition.get('match') == 'exact' else needle in text
        return self.runtime._transient_worker.call(sample, timeout=3)

    def _poll_native(self):
        seen_absent = not self.baseline
        try:
            while not self.stopped.wait(0.1):
                self.context.check()
                if self.started is not None and time.monotonic() >= self.started + self.timeout:
                    return
                sampled_at = time.monotonic()
                matched = self._sample_native()
                self.samples += 1
                if not self.dispatched.is_set() or sampled_at < self.started:
                    continue
                if time.monotonic() > self.started + self.timeout:
                    return
                if not matched:
                    seen_absent = True
                if seen_absent and matched:
                    self.result = {'status': 'met', 'baseline_present': self.baseline, 'samples': self.samples,
                                   'dispatched_at': self.dispatched_at, 'matched_at': timestamp(),
                                   'evidence': {'source': 'uia', 'text': self.condition['text']}}
                    self.context.change(expectation=self.result)
                    return
        except BaseException as error:
            self.error = error

    def dispatch(self, _index):
        self.started = time.monotonic()
        self.dispatched_at = timestamp()
        if self.target.kind == 'browser':
            self.frame.evaluate('''key => { const state = window[key];
              if (!state) throw new Error('Transient watcher disappeared');
              state.dispatch(); }''', self.key)
        self.dispatched.set()

    def wait(self):
        deadline = (self.started or time.monotonic()) + self.timeout
        while True:
            self.context.check()
            if self.error:
                raise self.error
            if self.target.kind == 'browser':
                state = self.frame.evaluate('''key => { const s = window[key];
                  return s && {baseline_present:s.baseline_present, samples:s.samples,
                    dispatched_at:s.dispatched_at, matched_at:s.matched_at}; }''', self.key)
                if state is None:
                    raise RuntimeFault('TARGET_CHANGED', 'Transient watcher lost its document')
                if state['matched_at']:
                    self.result = {'status': 'met', **state,
                                   'evidence': {'source': 'dom', 'text': self.condition['text']}}
                    self.context.change(expectation=self.result)
            if self.result:
                return {'expectation': self.result, 'outcome': 'expectation_met'}
            if time.monotonic() >= deadline:
                break
            self.context.cancelled.wait(min(0.1, max(0, deadline - time.monotonic())))
        result = {'status': 'timeout', 'baseline_present': self.baseline,
                  'samples': state['samples'] if self.target.kind == 'browser' else self.samples}
        if self.started is not None:
            result['dispatched_at'] = self.dispatched_at
        self.context.change(expectation=result)
        return {'expectation': result}

    def close(self):
        self.stopped.set()
        try:
            if self.target.kind == 'browser':
                try:
                    self.frame.evaluate('key => window[key]?.close()', self.key)
                except Exception:
                    pass
            if self.thread is not None:
                self.thread.join(timeout=3.1)
            if self.context.cancelled.is_set() and self.result is None:
                self.context.change(expectation={'status': 'cancelled', 'baseline_present': self.baseline})
        finally:
            self.runtime._transient_lock.release()


def validate_condition(condition):
    if not isinstance(condition, dict):
        raise ValueError('A condition object is required')
    kind = condition.get('kind')
    fields = {'text': {'text', 'match'}, 'element': {'ref', 'state'},
              'window': {'title', 'match'}, 'state': {'state'}}
    if kind not in fields or set(condition) - fields[kind] - {'kind'}:
        raise ValueError('Unsupported condition fields')
    if kind in ('text', 'window'):
        key = 'text' if kind == 'text' else 'title'
        if not isinstance(condition.get(key), str) or not condition[key] or len(condition[key]) > 4000:
            raise ValueError(key + ' must be a nonempty string up to 4000 characters')
        if condition.get('match', 'contains') not in ('contains', 'exact'):
            raise ValueError('match must be contains or exact')
    if kind == 'element':
        if not isinstance(condition.get('ref'), str) or not condition['ref']:
            raise ValueError('Element condition requires ref')
        if condition.get('state', 'visible') not in ('visible', 'hidden', 'enabled', 'disabled', 'checked', 'unchecked', 'focused'):
            raise ValueError('Unsupported element state')
    if kind == 'state' and condition.get('state') not in ('ready', 'closed', 'url_changed'):
        raise ValueError('Unsupported target state')


class Conditions:
    def __init__(self, runtime):
        self.runtime = runtime

    def sample(self, owner, target_id, oid, condition):
        kind = condition['kind']
        if kind == 'window':
            rows = self.runtime.target_directory(owner)
            needle = condition['title']
            found = next((row for row in rows if row['kind'] == 'window' and
                          (row['title'] == needle if condition.get('match') == 'exact' else needle in row['title'])), None)
            if not found:
                return False, {'evidence': {'windows_checked': len(rows)}}
            return True, self.runtime.observations.observe(owner, found['target_id'])
        try:
            target = self.runtime.resolve_target(owner, target_id)
        except RuntimeFault as error:
            if error.code == 'TARGET_CLOSED' and kind == 'state' and condition['state'] == 'closed':
                return True, {'closed': True, 'evidence': {'target_closed': True}}
            raise
        if kind == 'element':
            return self._element(owner, target, oid, condition)
        observed = self.runtime.observations.observe(owner, target_id, image=False)
        if kind == 'text':
            snapshot = self.runtime.observations.get(owner, observed['observation']['observation_id'])
            body = snapshot.text
            matched = body == condition['text'] if condition.get('match') == 'exact' else condition['text'] in body
            return matched, observed
        state = condition['state']
        if state == 'url_changed':
            baseline = self.runtime.observations.get(owner, oid, target_id)
            return observed['observation'].get('url') != baseline.metadata.get('url'), observed
        if state == 'ready':
            trace = observed['observation']['trace']
            if target.kind == 'browser':
                ready = self.runtime.browser.call(lambda: target.handle[1].evaluate('document.readyState'))
                matched = ready in ('interactive', 'complete') and not observed['observation']['warnings']
                observed['ready_evidence'] = {'document_ready_state': ready, 'dom_reads': trace['dom']}
            else:
                import ctypes
                matched = not ctypes.windll.user32.IsHungAppWindow(target.handle) and trace['uia'] == 1 and not observed['observation']['warnings']
                observed['ready_evidence'] = {'responsive': matched, 'uia_reads': trace['uia']}
            return matched, observed
        return False, observed

    def _element(self, owner, target, oid, condition):
        state = condition.get('state', 'visible')
        try:
            element, _ = self.runtime.observations.element(owner, oid, target.target_id, condition['ref'])
            if element.source == 'dom':
                def read():
                    node = self.runtime.observations.dom_element(owner, element)
                    try:
                        return {'visible': node.is_visible(), 'enabled': node.is_enabled(),
                                'checked': node.is_checked() if element.semantic.get('role') in ('checkbox', 'radio') else False,
                                'focused': node.evaluate('el => el.ownerDocument.activeElement === el')}
                    finally:
                        node.dispose()
                values = self.runtime.browser.call(read)
            elif element.source == 'uia':
                def read():
                    node = self.runtime.observations.validate_uia(owner, element, action='identity')['wrapper']
                    return {'visible': node.is_visible(), 'enabled': node.is_enabled(),
                            'checked': bool(node.get_toggle_state()) if element.semantic.get('role') == 'checkbox' else False,
                            'focused': node.has_keyboard_focus()}
                values = self.runtime.observations.uia.call(read)
            else:
                raise RuntimeFault('UNSUPPORTED_CONDITION', 'Visual reference cannot prove live element state')
        except RuntimeFault as error:
            # A renamed/replaced control is uncertain. Only actual removal may
            # satisfy hidden, so stale identity always requires fresh evidence.
            if state == 'hidden' and error.code == 'REF_STALE' and 'removed' in str(error):
                return True, {'evidence': {'ref': condition['ref'], 'removed': True}}
            raise
        opposite = {'hidden': 'visible', 'disabled': 'enabled', 'unchecked': 'checked'}
        matched = not values[opposite[state]] if state in opposite else values[state]
        return matched, {'evidence': {'ref': condition['ref'], **values}}

    def wait(self, context, target_id, oid, condition, timeout_ms):
        deadline = time.monotonic() + timeout_ms / 1000
        context.change(expectation={'status': 'pending'})
        result, samples = {}, 0
        while time.monotonic() < deadline:
            context.check()
            matched, result = self.sample(context.owner, target_id, oid, condition)
            samples += 1
            if matched:
                expectation = {'status': 'met', 'samples': samples}
                context.change(expectation=expectation)
                return {**result, 'expectation': expectation, 'outcome': 'expectation_met'}
            context.cancelled.wait(min(0.1, max(0, deadline - time.monotonic())))
        expectation = {'status': 'timeout', 'samples': samples}
        context.change(expectation=expectation)
        return {**result, 'expectation': expectation}
