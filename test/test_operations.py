import builtins
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
import tempfile
import threading
import time
import unittest
import json
import subprocess
import sys
import os
from types import SimpleNamespace
from unittest import mock

from dsh_desktop import input_gate
from dsh_desktop.actions import ActionDispatcher
from dsh_desktop.conditions import Conditions, TransientWatcher, validate_condition
from dsh_desktop.journal import OperationJournal
from dsh_desktop.operations import OperationManager, Cancelled
from dsh_desktop.targets import RuntimeFault
from dsh_desktop.worker import ApartmentWorker
from dsh_desktop.runtime import DesktopRuntime


class NativeInputTests(unittest.TestCase):
    def test_replace_uses_keyboard_events_even_when_value_pattern_exists(self):
        wrapper = mock.Mock()
        wrapper.is_enabled.return_value = True
        wrapper.has_keyboard_focus.return_value = True
        wrapper.element_info.rectangle = SimpleNamespace(left=0, top=0, right=100, bottom=20)
        input_service = mock.Mock()
        input_service.dispatch_context.return_value = nullcontext()
        runtime = SimpleNamespace(input=input_service)
        dispatcher = ActionDispatcher(runtime)
        dispatcher._native_guard = mock.Mock()
        dispatcher._verify_hit = mock.Mock()
        context = mock.Mock()
        target = SimpleNamespace(kind='window', metadata={})
        element = SimpleNamespace(source='uia', semantic={'editable': True})
        result = dispatcher._native(context, target, 'observation', 0,
                                    {'action': 'input', 'mode': 'replace', 'text': 'hello'},
                                    element, {'wrapper': wrapper})
        self.assertEqual(result, 'physical_input')
        wrapper.iface_value.SetValue.assert_not_called()
        wrapper.set_focus.assert_called_once()
        self.assertEqual(input_service.method_calls, [
            mock.call.dispatch_context(mock.ANY),
            mock.call.hotkey('ctrl', 'a'),
            mock.call.press_key('backspace', wait_time=0),
            mock.call.type_text('hello'),
        ])


class SemanticActionTests(unittest.TestCase):
    def test_uia_patterns_dispatch(self):
        wrapper = mock.Mock()
        wrapper.is_enabled.return_value = True
        dispatcher = ActionDispatcher(SimpleNamespace())
        dispatcher._native_guard = mock.Mock()
        context = mock.Mock()
        target = SimpleNamespace(kind='window')
        element = SimpleNamespace(source='uia')
        for action, fields in [('invoke', {}), ('set_value', {'value': 'hello'}), ('expand', {})]:
            step = {'action': action, 'target': {'ref': 'e'}, **fields}
            ActionDispatcher.validate({'target_id': 't', 'observation_id': 'o', 'steps': [step]})
            self.assertEqual(dispatcher._native(context, target, 'o', 0, step, element, {'wrapper': wrapper}), 'uia_' + action)
        wrapper.iface_invoke.Invoke.assert_called_once_with()
        wrapper.iface_value.SetValue.assert_called_once_with('hello')
        wrapper.iface_expand_collapse.Expand.assert_called_once_with()


class ClipboardActionTests(unittest.TestCase):
    def test_clipboard_set_get_and_file_paste(self):
        clipboard = mock.Mock()
        clipboard.CF_UNICODETEXT = 13
        clipboard.CF_HDROP = 15
        clipboard.IsClipboardFormatAvailable.return_value = True
        clipboard.GetClipboardData.return_value = 'hello'
        clipboard.RegisterClipboardFormat.return_value = 42
        input_service = mock.Mock()
        input_service.dispatch_context.return_value = nullcontext()
        dispatcher = ActionDispatcher(SimpleNamespace(input=input_service))
        dispatcher._native_guard = mock.Mock()
        context = mock.Mock()
        target = SimpleNamespace(kind='window')
        with mock.patch.dict(sys.modules, {'win32clipboard': clipboard}):
            result = dispatcher._global_step(context, target, 0, {'action': 'clipboard', 'op': 'set', 'text': 'hello'})
            self.assertEqual(result['op'], 'set')
            result = dispatcher._global_step(context, target, 1, {'action': 'clipboard', 'op': 'get'})
            self.assertEqual(result['text'], 'hello')
            with tempfile.NamedTemporaryFile() as file:
                result = dispatcher._global_step(context, target, 2, {'action': 'clipboard', 'op': 'paste_file', 'path': file.name})
                self.assertEqual(result['op'], 'paste_file')
                payload = next(call.args[1] for call in clipboard.SetClipboardData.call_args_list if call.args[0] == 15)
                self.assertEqual(payload[20:].decode('utf-16le'), str(Path(file.name).resolve()) + '\0\0')
            input_service.hotkey.assert_called_once_with('ctrl', 'v')
        self.assertEqual(clipboard.CloseClipboard.call_count, 3)

    def test_clipboard_argument_validation(self):
        base = {'target_id': 'target', 'observation_id': 'observation'}
        for step in ({'action': 'clipboard', 'op': 'get'},
                     {'action': 'clipboard', 'op': 'set', 'text': 'hello'}):
            ActionDispatcher.validate({**base, 'steps': [step]})
        for step in ({'action': 'clipboard', 'op': 'unknown'},
                     {'action': 'clipboard', 'op': 'set'},
                     {'action': 'clipboard', 'op': 'get', 'text': 'unexpected'}):
            with self.assertRaises(ValueError):
                ActionDispatcher.validate({**base, 'steps': [step]})


class FileActionTests(unittest.TestCase):
    def test_file_lifecycle_and_workspace_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            dispatcher = ActionDispatcher(SimpleNamespace(home=Path(directory)))
            context = mock.Mock()
            target = SimpleNamespace(kind='window')

            def run(op, **fields):
                step = {'action': 'file', 'op': op, **fields}
                ActionDispatcher.validate({'target_id': 't', 'observation_id': 'o', 'steps': [step]})
                return dispatcher._global_step(context, target, 0, step)

            run('create', path='samples/example.txt', text='one')
            self.assertEqual(run('read', path='samples/example.txt')['text'], 'one')
            run('write', path='samples/example.txt', text='two\r\nthree')
            self.assertEqual(run('read', path='samples/example.txt')['text'], 'two\r\nthree')
            self.assertEqual(run('list', path='samples')['entries'][0]['name'], 'example.txt')
            with self.assertRaises(RuntimeFault) as error:
                run('read', path='../outside.txt')
            self.assertEqual(error.exception.code, 'PATH_DENIED')
            run('delete', path='samples/example.txt')
            self.assertEqual(run('list', path='samples')['entries'], [])

    def test_file_argument_validation(self):
        base = {'target_id': 't', 'observation_id': 'o'}
        for step in ({'action': 'file', 'op': 'read'},
                     {'action': 'file', 'op': 'create', 'path': 'x'},
                     {'action': 'file', 'op': 'read', 'path': 'x', 'text': 'extra'}):
            with self.assertRaises(ValueError):
                ActionDispatcher.validate({**base, 'steps': [step]})


class JournalTests(unittest.TestCase):
    def test_restart_retains_completed_and_uncertain_steps_and_owner_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'operations.sqlite3'
            journal = OperationJournal(path, 'old')
            first, fresh = journal.register('a', 'call', 'digest', 'act')
            self.assertTrue(fresh)
            journal.change('a', first['operation_id'], status='running', dispatch='sent',
                           completed_steps=[0], in_flight_step=1)
            pending, _ = journal.register('a', 'queued', 'other', 'act')
            journal.close()
            journal = OperationJournal(path, 'new')
            try:
                replay, fresh = journal.register('a', 'call', 'digest', 'act')
                self.assertFalse(fresh)
                self.assertEqual(replay['status'], 'unknown')
                self.assertEqual(replay['uncertain_step'], 1)
                self.assertEqual(replay['completed_steps'], [0])
                self.assertEqual(replay['operation_id'], first['operation_id'])
                self.assertEqual(journal.get('a', pending['operation_id'])['dispatch'], 'none')
                with self.assertRaises(RuntimeFault):
                    journal.register('a', 'call', 'different', 'act')
                with self.assertRaises(RuntimeFault):
                    journal.get('b', first['operation_id'])
                other, fresh = journal.register('b', 'call', 'digest', 'act')
                self.assertTrue(fresh)
                self.assertNotEqual(other['operation_id'], first['operation_id'])
            finally:
                journal.close()
                journal.close()

    def test_full_result_expires_before_identity_and_does_not_reexecute(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = OperationJournal(Path(directory) / 'journal.db', 'epoch', result_ttl=10, identity_ttl=100)
            try:
                data, _ = journal.register('a', 'call', 'digest', 'act')
                journal.change('a', data['operation_id'], status='completed', result={'large': 'snapshot'})
                with mock.patch('dsh_desktop.journal.time.time', return_value=time.time() + 20):
                    journal.prune()
                replay, fresh = journal.register('a', 'call', 'digest', 'act')
                self.assertFalse(fresh)
                self.assertTrue(replay['result_expired'])
                self.assertNotIn('result', replay)
                with mock.patch('dsh_desktop.journal.time.time', return_value=time.time() + 110):
                    journal.prune()
                self.assertTrue(journal.register('a', 'call', 'digest', 'act')[1])
            finally:
                journal.close()

    def test_process_crash_before_and_after_dispatch_preserves_write_ahead_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            for dispatched in (False, True):
                path = Path(directory) / ('sent.db' if dispatched else 'accepted.db')
                effect = Path(directory) / 'effects.txt'
                script = '''
import sys
from pathlib import Path
from dsh_desktop.journal import OperationJournal
journal = OperationJournal(sys.argv[1], 'old')
data, _ = journal.register('owner', 'request', 'digest', 'act')
if sys.argv[3] == 'True':
    journal.change('owner', data['operation_id'], status='running', dispatch='sent', in_flight_step=0)
    Path(sys.argv[2]).write_text('step0\\n', encoding='utf-8')
    journal.change('owner', data['operation_id'], completed_steps=[0], in_flight_step=1)
    with open(sys.argv[2], 'a', encoding='utf-8') as stream:
        stream.write('step1\\n')
print('ready', flush=True)
sys.stdin.read()
'''
                child = subprocess.Popen([sys.executable, '-c', script, str(path), str(effect), str(dispatched)],
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    self.assertEqual(child.stdout.readline().strip(), 'ready')
                    child.kill()
                    child.communicate(timeout=5)
                    journal = OperationJournal(path, 'new')
                    try:
                        result, fresh = journal.register('owner', 'request', 'digest', 'act')
                        self.assertFalse(fresh)
                        self.assertEqual(result['status'], 'unknown' if dispatched else 'cancelled')
                        if dispatched:
                            self.assertEqual(result['completed_steps'], [0])
                            self.assertEqual(result['uncertain_step'], 1)
                            self.assertEqual(effect.read_text(encoding='utf-8').splitlines(), ['step0', 'step1'])
                    finally:
                        journal.close()
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.communicate(timeout=5)


class OperationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.runtime = SimpleNamespace(state_dir=Path(self.directory.name), epoch='fixture')
        self.manager = OperationManager(self.runtime)
        input_gate.clear_stop()

    def tearDown(self):
        self.manager.shutdown()
        self.directory.cleanup()

    def test_open_navigates_once_and_returns_initial_observation(self):
        self.runtime.operations = self.manager
        self.runtime._input_lock = threading.Lock()
        self.runtime.browser = SimpleNamespace(call=lambda function, **kwargs: function())
        self.runtime.open_target = mock.Mock(return_value=SimpleNamespace(target_id='t1', kind='browser', identity=('tab',)))
        self.runtime.observations = mock.Mock()
        self.runtime.observations.observe.return_value = {'success': True, 'observation': {'observation_id': 'o1'}}
        arguments = {'url': 'https://example.test', 'image': False}
        first = DesktopRuntime.open(self.runtime, 'a', 'open-key', arguments)
        replay = DesktopRuntime.open(self.runtime, 'a', 'open-key', arguments)
        self.assertEqual(first['status'], 'completed')
        self.assertEqual(first['outcome'], 'opened')
        self.assertEqual(first['target_id'], 't1')
        self.assertEqual(first['observation']['observation_id'], 'o1')
        self.assertEqual(first['operation_id'], replay['operation_id'])
        self.runtime.open_target.assert_called_once_with('a', url='https://example.test', target_id=None, call_id='open-key')
        self.runtime.observations.observe.assert_called_once_with('a', 't1', image=False)

    def test_slow_observe_returns_handle_and_retains_cancelled_worker(self):
        self.runtime.operations = self.manager
        self.runtime.targets = mock.Mock()
        self.runtime.targets.get.return_value = SimpleNamespace(kind='window')
        worker = ApartmentWorker('observe-fixture')
        entered, release = threading.Event(), threading.Event()
        def collect(*args, **kwargs):
            entered.set()
            release.wait(3)
            return {'success': True, 'observation': {'observation_id': 'late'}}
        self.runtime.observations = SimpleNamespace(uia=worker, observe=mock.Mock(side_effect=collect))
        try:
            result = DesktopRuntime.observe(self.runtime, 'a', 'read', {'target_id': 't'}, budget_ms=30)
            self.assertTrue(entered.wait(1))
            self.assertEqual(result['status'], 'running')
            self.assertEqual(result['dispatch'], 'none')
            self.assertTrue(result['worker_running'])
            replay = DesktopRuntime.observe(self.runtime, 'a', 'read', {'target_id': 't'}, budget_ms=0)
            self.assertEqual(result['operation_id'], replay['operation_id'])
            self.manager.stop('a', result['operation_id'])
            self.assertEqual(self.manager.status('a', result['operation_id'])['status'], 'cancelling')
            release.set()
            final = self.manager.status('a', result['operation_id'], follow_ms=2000)
            self.assertEqual(final['status'], 'cancelled')
            self.assertFalse(final['worker_running'])
            self.assertNotIn('observation', final)
            self.runtime.observations.observe.assert_called_once()
        finally:
            release.set()
            worker.shutdown()

    def test_open_hwnd_activates_and_returns_initial_observation_once(self):
        self.runtime.operations = self.manager
        self.runtime._input_lock = threading.Lock()
        self.runtime.window = mock.Mock()
        self.runtime.open_target = mock.Mock(return_value=SimpleNamespace(
            target_id='native', kind='window', identity='42', handle=42))
        self.runtime.observations = mock.Mock()
        self.runtime.observations.uia.call.side_effect = lambda function, **kwargs: function()
        self.runtime.observations.observe.return_value = {'success': True, 'observation': {'observation_id': 'o'}}
        first = DesktopRuntime.open(self.runtime, 'a', 'hwnd-open', {'hwnd': 42})
        replay = DesktopRuntime.open(self.runtime, 'a', 'hwnd-open', {'hwnd': 42})
        self.assertEqual(first['status'], 'completed')
        self.assertEqual(first['target_id'], 'native')
        self.assertEqual(first['operation_id'], replay['operation_id'])
        self.runtime.open_target.assert_called_once_with('a', hwnd=42)
        self.runtime.window.force_foreground.assert_called_once_with(42)

    def test_observe_rejects_invalid_arguments_before_operation_registration(self):
        self.runtime.operations = self.manager
        for arguments in ({'unknown': 1}, {'image': 1}, {'target_id': ''}, {'query': ''},
                          {'region': {}}, {'cursor': 'o.0.0', 'image': True}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                DesktopRuntime.observe(self.runtime, 'a', 'invalid-read', arguments)
        self.assertEqual(self.manager.journal.active('a'), [])

    def test_open_rejects_conflicting_or_invalid_arguments(self):
        self.runtime.operations = self.manager
        for arguments in ({}, {'app': 'notepad', 'url': 'https://example.test'},
                          {'connect': 'configured', 'target_id': 't'}, {'args': []},
                          {'url': 'https://example.test', 'image': 1}, {'hwnd': True}, {'hwnd': 0},
                          {'hwnd': '42'}, {'hwnd': 1.5}, {'hwnd': 42, 'app': 'notepad'},
                          {'hwnd': 42, 'target_id': 't'}, {'hwnd': 42, 'args': []}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                DesktopRuntime.open(self.runtime, 'a', 'key', arguments)

    def test_open_tracks_cancelled_backend_until_navigation_returns(self):
        self.runtime.operations = self.manager
        self.runtime._input_lock = threading.Lock()
        entered, release = threading.Event(), threading.Event()
        worker = ApartmentWorker('open-fixture')
        self.runtime.browser = SimpleNamespace(call=worker.call)
        self.runtime.observations = mock.Mock()
        def navigate(*args, **kwargs):
            entered.set()
            release.wait(3)
            return SimpleNamespace(target_id='t1', kind='browser', identity=('tab',))
        self.runtime.open_target = navigate
        try:
            opened = DesktopRuntime.open(self.runtime, 'a', 'open-cancel', {'url': 'https://example.test'}, budget_ms=30)
            self.assertTrue(entered.wait(1))
            context = self.manager._contexts[opened['operation_id']]
            self.assertTrue(context.backend_running)
            self.manager.stop('a', opened['operation_id'])
            self.assertTrue(self.runtime._input_lock.locked())
            release.set()
            result = self.manager.status('a', opened['operation_id'], follow_ms=2000)
            self.assertEqual(result['status'], 'cancelled')
            self.assertEqual(result['completed_steps'], [0])
            self.runtime.observations.observe.assert_not_called()
        finally:
            release.set()
            worker.shutdown()

    def test_wait_timeout_and_idempotent_replay(self):
        self.runtime.operations = self.manager
        arguments = {'target_id': 't', 'condition': {'kind': 'text', 'text': 'done'}, 'timeout_ms': 100}
        with mock.patch.object(Conditions, 'sample', return_value=(False, {'evidence': {'text': 'pending'}})) as sample:
            first = DesktopRuntime.wait(self.runtime, 'a', 'wait-key', arguments)
            calls = sample.call_count
            replay = DesktopRuntime.wait(self.runtime, 'a', 'wait-key', arguments)
        self.assertEqual(first['status'], 'failed')
        self.assertEqual(first['error']['code'], 'EXPECTATION_TIMEOUT')
        self.assertEqual(first['evidence']['text'], 'pending')
        self.assertEqual(first['operation_id'], replay['operation_id'])
        self.assertEqual(sample.call_count, calls)

    def test_wait_validates_references_before_submitting(self):
        self.runtime.operations = self.manager
        invalid = [
            {'condition': {'kind': 'text', 'text': 'done'}},
            {'target_id': 't', 'condition': {'kind': 'element', 'ref': 'e'}},
            {'target_id': 't', 'condition': {'kind': 'state', 'state': 'url_changed'}},
            {'condition': {'kind': 'window', 'title': 'x'}, 'timeout_ms': True},
        ]
        for arguments in invalid:
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                DesktopRuntime.wait(self.runtime, 'a', 'invalid', arguments)

    def test_retry_and_concurrent_retry_dispatch_exactly_once(self):
        count = []
        def execute(context):
            context.dispatch(0)
            count.append(1)
            time.sleep(0.03)
            context.completed(0, 'fixture')
            return {'value': 42}
        with ThreadPoolExecutor(max_workers=8) as pool:
            calls = list(pool.map(lambda _: self.manager.submit('a', 'same', 'act', {}, execute), range(8)))
        self.assertEqual(count, [1])
        self.assertEqual(len({value['operation_id'] for value in calls}), 1)
        self.assertEqual(calls[0]['completed_steps'], [0])
        self.assertEqual(calls[0]['value'], 42)

    def test_partial_cancel_preserves_known_steps_and_follow_cancel_is_query_only(self):
        completed, release = threading.Event(), threading.Event()
        def execute(context):
            context.dispatch(0)
            context.completed(0, 'fixture')
            completed.set()
            release.wait(1)
            context.check()
            raise AssertionError('Cancelled second step executed')
        result = self.manager.submit('a', 'partial', 'act', {}, execute, budget_ms=0)
        self.assertTrue(completed.wait(1))
        query = threading.Event()
        query.set()
        with self.assertRaises(Cancelled):
            self.manager.status('a', result['operation_id'], follow_ms=1000, cancelled=query)
        self.assertFalse(self.manager._contexts[result['operation_id']].cancelled.is_set())
        self.manager.stop('a', result['operation_id'])
        release.set()
        done = self.manager.status('a', result['operation_id'], follow_ms=1000)
        self.assertEqual(done['status'], 'cancelled')
        self.assertEqual(done['completed_steps'], [0])
        self.assertEqual(done['dispatch'], 'partial')

    def test_backend_timeout_revokes_actual_worker_and_keeps_input_lock_until_return(self):
        worker = ApartmentWorker('operation-test-uia')
        worker.call(lambda: None)
        started, release = threading.Event(), threading.Event()
        lock, late_inputs = threading.Lock(), []
        def execute(context):
            def backend():
                with context.hold(lock):
                    context.dispatch(0)
                    started.set()
                    release.wait(2)
                    context.guard_input()
                    late_inputs.append(1)
            return context.backend(worker.call, backend, timeout=0.05)
        try:
            result = self.manager.submit('a', 'timeout', 'act', {}, execute)
            self.assertTrue(started.is_set())
            self.assertEqual(result['status'], 'unknown')
            self.assertEqual(result['uncertain_step'], 0)
            self.assertTrue(result['worker_running'])
            self.assertFalse(lock.acquire(blocking=False))
            release.set()
            deadline = time.monotonic() + 2
            while self.manager._contexts and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(self.manager.status('a', result['operation_id'])['worker_running'])
            self.assertEqual(late_inputs, [])
            self.assertTrue(lock.acquire(blocking=False))
            lock.release()
        finally:
            release.set()
            worker.shutdown()

    def test_target_and_global_input_locks_serialize_owners(self):
        shared = threading.Lock()
        active, maximum = 0, 0
        targets = [SimpleNamespace(kind='window', identity=42) for _ in range(2)]
        target_lock = self.manager.target_lock(targets[0])
        self.assertIs(target_lock, self.manager.target_lock(targets[1]))
        def execute(context):
            nonlocal active, maximum
            with context.hold(target_lock), context.hold(shared):
                active += 1
                maximum = max(maximum, active)
                time.sleep(0.03)
                active -= 1
            return {}
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.manager.submit, owner, 'call', 'act', {}, execute) for owner in ('a', 'b')]
            self.assertTrue(all(future.result()['status'] == 'completed' for future in futures))
        self.assertEqual(maximum, 1)

    def test_dispatch_failure_is_unknown_and_pre_dispatch_failure_is_failed(self):
        def unknown(context):
            context.dispatch(0)
            raise OSError('response lost')
        def failed(context):
            raise RuntimeFault('REF_STALE', 'fixture replaced')
        first = self.manager.submit('a', 'one', 'act', {}, unknown)
        second = self.manager.submit('a', 'two', 'act', {}, failed)
        self.assertEqual(first['status'], 'unknown')
        self.assertNotIn('error', first)
        self.assertEqual(second['status'], 'failed')
        self.assertEqual(second['error']['code'], 'REF_STALE')

    def test_invalid_budget_does_not_create_a_persistent_request(self):
        with self.assertRaises(ValueError):
            self.manager.submit('a', 'call', 'act', {}, lambda context: {}, budget_ms=-1)
        with self.assertRaises(RuntimeFault):
            self.manager.status('a', request_key='call')

    def test_queued_cancellation_releases_capacity_without_running_callback(self):
        release, all_started = threading.Event(), threading.Event()
        started = []
        def blocked(context):
            started.append(context.operation_id)
            if len(started) == 4:
                all_started.set()
            release.wait(2)
            context.check()
            return {}
        try:
            for index in range(4):
                self.manager.submit('a', 'active-' + str(index), 'act', {}, blocked, budget_ms=0)
            self.assertTrue(all_started.wait(1))
            callback = mock.Mock(return_value={})
            queued = self.manager.submit('b', 'queued', 'act', {}, callback, budget_ms=0)
            self.manager.stop('b', queued['operation_id'])
            self.assertEqual(self.manager.status('b', queued['operation_id'])['status'], 'cancelled')
            self.assertNotIn(queued['operation_id'], self.manager._contexts)
            callback.assert_not_called()
        finally:
            release.set()

    def test_batch_stale_second_step_retains_first_dispatch_and_current_readback(self):
        target = SimpleNamespace(kind='desktop', identity='desktop', target_id='target', metadata={})
        self.runtime.resolve_target = lambda owner, target_id: target
        self.runtime.observations = mock.Mock()
        self.runtime.observations.get.return_value = SimpleNamespace(metadata={})
        self.runtime.observations.capture_frame.side_effect = RuntimeFault('FRAME_UNAVAILABLE', 'fixture')
        self.runtime.operations = self.manager
        self.runtime._input_lock = threading.Lock()
        dispatcher = ActionDispatcher(self.runtime)
        dispatcher._boundary = mock.Mock(return_value=('stable',))
        dispatcher._readback = mock.Mock(return_value={'observation': {'text': 'actual state'}})
        inputs = []
        def step(context, target, oid, index, arguments):
            if index == 1:
                raise RuntimeFault('REF_STALE', 'fixture replaced')
            context.dispatch(index)
            inputs.append(index)
            return 'fixture'
        dispatcher._step = step
        arguments = {'target_id': 'target', 'observation_id': 'snapshot', 'steps': [{'action': 'click'}] * 2}
        result = self.manager.submit('a', 'batch', 'act', arguments, lambda context: dispatcher._batch(context, arguments))
        self.assertEqual(inputs, [0])
        self.assertEqual(result['completed_steps'], [0])
        self.assertEqual(result['stopped_at'], 1)
        self.assertEqual(result['dispatch'], 'partial')
        self.assertEqual(result['error']['code'], 'REF_STALE')
        self.assertEqual(result['observation']['text'], 'actual state')

    def test_long_operation_returns_a_handle_before_completion(self):
        release = threading.Event()
        def execute(context):
            release.wait(1)
            return {'value': 7}
        try:
            start = time.monotonic()
            result = self.manager.submit('a', 'long', 'observe', {}, execute, budget_ms=30)
            self.assertLess(time.monotonic() - start, 0.5)
            self.assertEqual(result['status'], 'running')
            release.set()
            self.assertEqual(self.manager.status('a', result['operation_id'], follow_ms=1000)['value'], 7)
        finally:
            release.set()

    def test_original_request_cancellation_revokes_operation(self):
        started, cancel, release = threading.Event(), threading.Event(), threading.Event()
        def execute(context):
            started.set()
            release.wait(1)
            context.check()
            raise AssertionError('cancelled execution continued')
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self.manager.submit, 'a', 'original', 'act', {}, execute,
                                     budget_ms=1000, cancelled=cancel)
                self.assertTrue(started.wait(1))
                cancel.set()
                with self.assertRaises(Cancelled):
                    future.result(timeout=1)
                release.set()
                result = self.manager.status('a', request_key='original', follow_ms=1000)
                self.assertEqual(result['status'], 'cancelled')
        finally:
            release.set()


class ActionValidationTests(unittest.TestCase):
    def test_rebound_status_exposes_current_target_and_retains_original_identity(self):
        manager = mock.Mock()
        manager._changed = threading.Condition()
        manager._validate_follow = OperationManager._validate_follow
        manager.journal.get.return_value = {
            'status': 'completed', 'target_id': 'old',
            'result': {'rebound': True, 'target_id': 'new', 'observation': {'target_id': 'new'}}}
        result = OperationManager.status(manager, 'a', 'operation')
        self.assertEqual(result['target_id'], 'new')
        self.assertEqual(result['original_target_id'], 'old')
        self.assertEqual(result['observation']['target_id'], 'new')

    def test_local_click_evidence_detects_low_contrast_change_and_retains_crops(self):
        import cv2
        import numpy as np
        from dsh_desktop.geometry import FrameGeometry

        runtime = mock.Mock()
        dispatcher = ActionDispatcher(runtime)
        runtime.observations.previous_click.return_value = None
        before = np.full((904, 1366, 3), 240, dtype=np.uint8)
        after = before.copy()
        after[400:440, 650:750] = 225
        images = [cv2.imencode('.jpg', image)[1].tobytes() for image in (before, after)]
        geometry = FrameGeometry((0, 0, 1366, 904), (1366, 904))
        runtime.observations.get.side_effect = [SimpleNamespace(frames=[SimpleNamespace(frame_id=frame_id, geometry=geometry)])
                                                for frame_id in ('before', 'after')]
        runtime.observations.image.side_effect = images
        runtime.observations.crop_frame.side_effect = [{'frame_id': 'local-before'}, {'frame_id': 'local-after'}]
        result = dispatcher._click_evidence('a', mock.Mock(), (700, 400),
            {'observation': {'frames': [{'frame_id': 'before', 'observation_id': 'o-before'}]}},
            {'frame_id': 'after', 'observation_id': 'o-after'})
        global_result = dispatcher._compare_visual_effect(*images)
        self.assertFalse(global_result['visual_effect'])
        self.assertTrue(result['local_visual_effect'])
        self.assertGreater(result['local_visual_effect_changed_ratio'], 0.005)
        self.assertIsNone(result['popup_detected'])
        self.assertEqual(result['click_region_frames']['before']['frame_id'], 'local-before')
        self.assertEqual(result['local_sample_region'],
                         {'frame_id': 'after', 'x': 500, 'y': 200, 'width': 400, 'height': 400})
        self.assertIn('toggle', result['next_action'])
        runtime.observations.get.side_effect = [SimpleNamespace(frames=[SimpleNamespace(frame_id=frame_id, geometry=geometry)])
                                                for frame_id in ('before', 'after')]
        runtime.observations.image.side_effect = images[::-1]
        runtime.observations.previous_click.return_value = tuple(images)
        runtime.observations.crop_frame.side_effect = [{'frame_id': 'local-before'}, {'frame_id': 'local-after'}]
        reversed_result = dispatcher._click_evidence('a', mock.Mock(), (700, 400),
            {'observation': {'frames': [{'frame_id': 'before', 'observation_id': 'o-before'}]}},
            {'frame_id': 'after', 'observation_id': 'o-after'})
        self.assertTrue(reversed_result['toggle'])
        self.assertEqual(reversed_result['toggle_evidence']['scope'], 'local_pixels')

    def test_local_click_evidence_clips_edges_and_ignores_distant_changes(self):
        import cv2
        import numpy as np
        from dsh_desktop.geometry import FrameGeometry

        runtime = mock.Mock()
        dispatcher = ActionDispatcher(runtime)
        runtime.observations.previous_click.return_value = None
        before = np.full((400, 800, 3), 240, dtype=np.uint8)
        after = before.copy()
        after[:, 500:] = 0
        geometry = FrameGeometry((-100, 20, 1600, 800), (800, 400))
        runtime.observations.get.side_effect = [SimpleNamespace(frames=[SimpleNamespace(frame_id=frame_id, geometry=geometry)])
                                                for frame_id in ('before', 'after')]
        runtime.observations.image.side_effect = [cv2.imencode('.jpg', image)[1].tobytes() for image in (before, after)]
        result = dispatcher._click_evidence('a', mock.Mock(), (-100, 20),
            {'observation': {'frames': [{'frame_id': 'before', 'observation_id': 'o-before'}]}},
            {'frame_id': 'after', 'observation_id': 'o-after'})
        self.assertFalse(result['local_visual_effect'])
        self.assertEqual(result['local_visual_effect_changed_ratio'], 0)
        self.assertEqual(result['local_sample_region'],
                         {'frame_id': 'after', 'x': 0, 'y': 0, 'width': 100, 'height': 100})

    def test_visual_effect_uses_supported_decode_flag_and_json_boolean(self):
        import cv2
        import numpy as np

        dispatcher = ActionDispatcher(mock.Mock())
        before = np.zeros((40, 40, 3), dtype=np.uint8)
        after = np.full((40, 40, 3), 255, dtype=np.uint8)
        unchanged_ok, unchanged = cv2.imencode('.jpg', before)
        changed_ok, changed = cv2.imencode('.jpg', after)
        self.assertTrue(unchanged_ok and changed_ok)

        unchanged_result = dispatcher._compare_visual_effect(
            unchanged.tobytes(), unchanged.tobytes())
        changed_result = dispatcher._compare_visual_effect(
            unchanged.tobytes(), changed.tobytes())

        self.assertIs(type(unchanged_result['visual_effect']), bool)
        self.assertIs(type(changed_result['visual_effect']), bool)
        self.assertNotIn('visual_effect_reason', unchanged_result)
        self.assertNotIn('visual_effect_reason', changed_result)
        self.assertEqual(unchanged_result['visual_effect_changed_ratio'], 0.0)
        self.assertGreater(changed_result['visual_effect_changed_ratio'], 0.005)
        self.assertFalse(unchanged_result['visual_effect'])
        self.assertTrue(changed_result['visual_effect'])
        self.assertEqual(json.loads(json.dumps(changed_result)), changed_result)

    def test_visual_effect_comparison_reports_unknown_for_invalid_or_different_frames(self):
        import cv2
        import numpy as np

        dispatcher = ActionDispatcher(mock.Mock())
        small_ok, small = cv2.imencode('.jpg', np.zeros((40, 40, 3), dtype=np.uint8))
        large_ok, large = cv2.imencode('.jpg', np.zeros((60, 40, 3), dtype=np.uint8))
        self.assertTrue(small_ok and large_ok)

        for before, after, reason in ((b'invalid', small.tobytes(), 'decode_failed'),
                                      (small.tobytes(), large.tobytes(), 'shape_mismatch')):
            result = dispatcher._compare_visual_effect(before, after)
            self.assertIsNone(result['visual_effect'])
            self.assertEqual(result['visual_effect_reason'], reason)
            self.assertNotIn('visual_effect_changed_ratio', result)

    def test_visual_effect_is_unknown_when_optional_vision_packages_are_unavailable(self):
        original_import = builtins.__import__

        def without_vision(name, *args, **kwargs):
            if name in ('cv2', 'numpy'):
                raise ImportError('optional visual dependency unavailable')
            return original_import(name, *args, **kwargs)

        dispatcher = ActionDispatcher(mock.Mock())
        with mock.patch('builtins.__import__', side_effect=without_vision):
            result = dispatcher._compare_visual_effect(b'image', b'image')

        self.assertIsNone(result['visual_effect'])
        self.assertEqual(result['visual_effect_reason'], 'comparison_unavailable')

    def test_readback_preserves_unknown_visual_effect_and_reason(self):
        runtime = mock.Mock()
        runtime.observations.observe.return_value = {
            'observation': {'observation_id': 'after', 'frames': [{'frame_id': 'after-frame'}]}}
        runtime.observations.get.return_value = SimpleNamespace(text='', elements=[])
        runtime.observations.image.side_effect = [b'before', b'after']
        dispatcher = ActionDispatcher(runtime)
        before_observation = {'observation': {'frames': [{'frame_id': 'before-frame'}]}}
        with mock.patch.object(dispatcher, '_compare_visual_effect',
                               return_value={'visual_effect': None, 'visual_effect_reason': 'shape_mismatch'}):
            result = dispatcher._readback('owner', 'target', None,
                                          before_observation=before_observation, action='click')

        self.assertIsNone(result['visual_effect'])
        self.assertEqual(result['visual_effect_reason'], 'shape_mismatch')

    def test_transient_watcher_is_armed_before_dispatch_and_closed(self):
        order = []
        runtime = mock.Mock()
        target = SimpleNamespace(kind='browser')
        runtime.resolve_target.return_value = target
        runtime.observations.get.return_value = SimpleNamespace(metadata={})
        context = mock.Mock()
        context.owner = 'a'
        context.hold.side_effect = lambda _lock: nullcontext()
        dispatcher = ActionDispatcher(runtime)
        dispatcher._boundary = mock.Mock(return_value=('unchanged',))
        dispatcher._readback = mock.Mock(return_value={})
        def step(*_args):
            order.append('step')
            context.before_dispatch(0)
            return 'dom_click'
        dispatcher._step = step
        watcher = mock.Mock()
        watcher.dispatch.side_effect = lambda _index: order.append('dispatch')
        watcher.wait.return_value = {'expectation': {'status': 'met'}}
        watcher.close.side_effect = lambda: order.append('closed')
        def arm(*_args, **_kwargs):
            order.append('armed')
            return watcher
        arguments = {'target_id': 't', 'observation_id': 'o', 'steps': [{'action': 'click', 'target': {'ref': 'e'}}],
                     'expect': {'kind': 'text', 'text': 'saved', 'transient': True}}
        with mock.patch('dsh_desktop.actions.TransientWatcher', side_effect=arm):
            result = dispatcher._batch(context, arguments)
        self.assertEqual(result['expectation']['status'], 'met')
        self.assertEqual(order, ['armed', 'step', 'dispatch', 'closed'])
        self.assertIsNone(context.before_dispatch)

    def test_failed_transient_arm_does_not_prevent_dispatch(self):
        runtime = mock.Mock()
        runtime._transient_lock = threading.Lock()
        runtime.resolve_target.return_value = SimpleNamespace(kind='browser')
        runtime.observations.get.return_value = SimpleNamespace(metadata={})
        context = mock.Mock()
        context.owner = 'a'
        context.hold.side_effect = lambda _lock: nullcontext()
        dispatcher = ActionDispatcher(runtime)
        dispatcher._boundary = mock.Mock(return_value=('unchanged',))
        dispatcher._readback = mock.Mock(return_value={})
        dispatcher._step = mock.Mock(return_value='dom_click')
        arguments = {'target_id': 't', 'observation_id': 'o',
                     'steps': [{'action': 'click', 'target': {'ref': 'e'}}],
                     'expect': {'kind': 'text', 'text': 'saved', 'transient': True}}
        def failed_arm(*args, **kwargs):
            runtime._transient_lock.release()
            raise RuntimeFault('UIA_BATCH_TIMEOUT', 'scan timed out')
        with mock.patch('dsh_desktop.actions.TransientWatcher', side_effect=failed_arm):
            result = dispatcher._batch(context, arguments)
        dispatcher._step.assert_called_once()
        self.assertTrue(result['success'])
        self.assertEqual(result['action']['status'], 'dispatched')
        self.assertEqual(result['verification']['status'], 'unknown')
        self.assertIn('scan timed out', result['expectation']['reason'])
        self.assertFalse(runtime._transient_lock.locked())

    def test_transient_native_watcher_records_reappearance_after_dispatch(self):
        runtime = SimpleNamespace(_transient_lock=threading.Lock())
        context = mock.Mock()
        context.cancelled = threading.Event()
        target = SimpleNamespace(kind='window')
        with mock.patch.object(TransientWatcher, '_sample_native', side_effect=[True, False, True]):
            watcher = TransientWatcher(runtime, context, target, {'kind': 'text', 'text': 'saved', 'timeout_ms': 1000})
            try:
                watcher.dispatch(0)
                result = watcher.wait()
            finally:
                watcher.close()
        self.assertEqual(result['expectation']['status'], 'met')
        self.assertTrue(result['expectation']['baseline_present'])
        self.assertEqual(result['expectation']['evidence']['source'], 'uia')
        self.assertFalse(runtime._transient_lock.locked())

    def test_independent_watch_collects_multiple_native_appearances(self):
        runtime = SimpleNamespace(_transient_lock=threading.Lock())
        context = mock.Mock(cancelled=threading.Event())
        samples = iter([False, True, False, True])
        with mock.patch.object(TransientWatcher, '_sample_native', side_effect=lambda: next(samples, False)):
            watcher = TransientWatcher(runtime, context, SimpleNamespace(kind='window'),
                                       {'kind': 'transient_watch', 'text': 'toast', 'timeout_ms': 650})
            try:
                watcher.dispatch(0)
                result = watcher.wait()['expectation']
            finally:
                watcher.close()
        self.assertEqual(result['status'], 'met')
        self.assertEqual(len(result['timeline']), 2)
        self.assertFalse(runtime._transient_lock.locked())

    def test_wait_entry_closes_independent_watcher_on_cancellation(self):
        runtime = mock.Mock()
        runtime.resolve_target.return_value = SimpleNamespace(kind='browser')
        context = mock.Mock()
        runtime.operations.submit.side_effect = lambda *a, **kw: a[4](context)
        with mock.patch('dsh_desktop.conditions.TransientWatcher') as factory:
            factory.return_value.wait.side_effect = Cancelled('cancel')
            with self.assertRaises(Cancelled):
                DesktopRuntime.wait(runtime, 'owner', 'key', {'target_id': 't',
                    'condition': {'kind': 'transient_watch', 'text': 'toast'}, 'duration_ms': 90000})
            self.assertTrue(factory.call_args.kwargs['browser_calls'])
            factory.return_value.close.assert_called_once()

    def test_browser_independent_watch_yields_worker_and_releases_resources(self):
        frame = mock.Mock()
        frame.evaluate.side_effect = [{'baseline_present': False}, None,
            {'baseline_present': False, 'samples': 2, 'dispatched_at': 'now', 'matched_at': None,
             'timeline': [], 'dropped': 0}, None]
        runtime = SimpleNamespace(_transient_lock=threading.Lock(),
                                  browser=SimpleNamespace(call=lambda f: f()))
        context = mock.Mock(cancelled=threading.Event())
        watcher = TransientWatcher(runtime, context, SimpleNamespace(kind='browser', handle=(None, frame)),
                                   {'kind': 'transient_watch', 'text': 'toast', 'timeout_ms': 1}, browser_calls=True)
        self.assertFalse(runtime._transient_lock.locked())
        try:
            watcher.dispatch(0)
            with mock.patch('dsh_desktop.conditions.time.monotonic', return_value=watcher.started + 1):
                self.assertEqual(watcher.wait()['expectation']['timeline'], [])
        finally:
            watcher.close()
        self.assertIn('close()', frame.evaluate.call_args.args[0])

    def test_invalid_batch_and_point_inputs_fail_before_execution(self):
        base = {'target_id': 't', 'observation_id': 'o'}
        invalid = [[], [{'action': 'close'}] * 9, [{'action': 'click', 'target': {'ref': 'e', 'x': 1}}],
                   [{'action': 'click', 'target': {'frame_id': 'f', 'x': float('nan'), 'y': 1}}],
                   [{'action': 'input', 'target': {'ref': 'e'}, 'text': 'hello'}],
                   [{'action': 'key', 'keys': []}], [{'action': 'scroll', 'target': {'ref': 'e'}, 'direction': 'down', 'amount': True}]]
        for steps in invalid:
            with self.subTest(steps=steps), self.assertRaises(ValueError):
                ActionDispatcher.validate({**base, 'steps': steps})
        ActionDispatcher.validate({**base, 'steps': [{'action': 'input', 'target': {'ref': 'e'}, 'text': '', 'mode': 'replace'}]})
        with self.assertRaisesRegex(ValueError, r'Provide each key separately.*CTRL.*O'):
            ActionDispatcher.validate({**base, 'steps': [{'action': 'key', 'keys': ['ctrl+o']}]})
        ActionDispatcher.validate({**base, 'steps': [{'action': 'key', 'keys': ['CTRL', 'O']}]})
        ActionDispatcher.validate({**base, 'steps': [{'action': 'key', 'keys': ['CTRL', 'S']}]})

    def test_condition_validation_prevents_incomplete_post_dispatch_checks(self):
        for condition in ({'kind': 'text'}, {'kind': 'window', 'title': ''}, {'kind': 'state', 'state': 'imaginary'},
                          {'kind': 'element', 'ref': 'e', 'state': 'imaginary'}):
            with self.subTest(condition=condition), self.assertRaises(ValueError):
                validate_condition(condition)
        for condition in ({'kind': 'text', 'text': '保存'}, {'kind': 'window', 'title': 'fixture'},
                          {'kind': 'state', 'state': 'closed'}, {'kind': 'element', 'ref': 'e', 'state': 'checked'}):
            validate_condition(condition)

    def test_text_condition_uses_full_snapshot_and_closed_condition_has_live_evidence(self):
        runtime = mock.Mock()
        runtime.observations.observe.return_value = {'observation': {'observation_id': 'fresh', 'text': 'first page'}}
        runtime.observations.get.return_value = SimpleNamespace(text='first page\nlater page 保存成功')
        conditions = Conditions(runtime)
        self.assertTrue(conditions.sample('a', 't', 'o', {'kind': 'text', 'text': '保存成功'})[0])
        runtime.resolve_target.side_effect = RuntimeFault('TARGET_CLOSED', 'fixture closed')
        matched, evidence = conditions.sample('a', 't', 'o', {'kind': 'state', 'state': 'closed'})
        self.assertTrue(matched)
        self.assertTrue(evidence['closed'])

    def test_runtime_observation_and_frame_endpoints(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from dsh_desktop.dsh_bridge import create_router

        token = 't' * 32
        owner = 'a' * 64
        runtime = mock.Mock()
        def image(request_owner, frame_id):
            if request_owner != owner or frame_id != 'f_1':
                raise RuntimeFault('FRAME_EXPIRED', 'Image resource has expired')
            return b'jpeg fixture'
        runtime.observations.image.side_effect = image
        with mock.patch.dict(os.environ, {'DSH_DESKTOP_BRIDGE_TOKEN': token}):
            app = FastAPI()
            app.include_router(create_router(runtime))
        headers = {'Authorization': 'Bearer ' + token, 'X-DSH-Owner': owner}
        with TestClient(app) as client:
            response = client.get('/api/dsh/runtime/frames/f_1', headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, b'jpeg fixture')
            self.assertEqual(response.headers['content-type'], 'image/jpeg')
            self.assertEqual(client.get('/api/dsh/runtime/frames/f_1', headers={**headers, 'X-DSH-Owner': 'b' * 64}).status_code, 404)
            self.assertEqual(client.get('/api/dsh/runtime/frames/f_1', headers={**headers, 'Authorization': 'Bearer wrong'}).status_code, 401)
            runtime.observe.return_value = {'success': True, 'targets': []}
            payload = {'request_key': 'read-1', 'arguments': {'image': False}}
            response = client.post('/api/dsh/runtime/observe', headers=headers, json=payload)
            self.assertEqual(response.json(), {'success': True, 'targets': []})
            self.assertEqual(runtime.observe.call_args.args, (owner, 'read-1', {'image': False}))
            self.assertEqual(client.post('/api/dsh/runtime/observe', headers=headers, json={'unknown': 1}).status_code, 422)
            self.assertEqual(client.post('/api/dsh/runtime/observe', headers={**headers, 'Authorization': 'Bearer wrong'}, json={}).status_code, 401)
            runtime.observe.side_effect = ValueError('invalid observation')
            self.assertEqual(client.post('/api/dsh/runtime/observe', headers=headers, json=payload).status_code, 422)
            runtime.observe.side_effect = RuntimeFault('TARGET_CLOSED', 'fixture closed')
            response = client.post('/api/dsh/runtime/observe', headers=headers, json=payload)
            self.assertEqual(response.json()['error']['code'], 'TARGET_CLOSED')
            runtime.wait.return_value = {'success': True, 'status': 'completed'}
            response = client.post('/api/dsh/runtime/wait', headers=headers,
                                   json={'request_key': 'call-1', 'arguments': {'condition': {'kind': 'window', 'title': 'x'}}})
            self.assertEqual(response.json()['status'], 'completed')
            self.assertEqual(runtime.wait.call_args.args[:2], (owner, 'call-1'))
            self.assertEqual(client.post('/api/dsh/runtime/wait', headers=headers, json={}).status_code, 422)


if __name__ == '__main__':
    unittest.main()
