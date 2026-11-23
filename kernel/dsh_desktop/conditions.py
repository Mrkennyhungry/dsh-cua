"""Finite, observable conditions shared by action expectations and waits."""
import time

from .targets import RuntimeFault


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
