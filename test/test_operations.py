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
                          {'url': 'https://example.test', 'image': 1}):
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
    def test_visual_effect_uses_supported_decode_flag_and_json_boolean(self):
        import cv2
        import numpy as np

        dispatcher = ActionDispatcher(mock.Mock())
        before = np.zeros((40, 40, 3), dtype=np.uint8)
        after = np.full((40, 40, 3), 255, dtype=np.uint8)
        unchanged_ok, unchanged = cv2.imencode('.jpg', before)
        changed_ok, changed = cv2.imencode('.jpg', after)
        self.assertTrue(unchanged_ok and changed_ok)

        unchanged_result, unchanged_reason, unchanged_ratio = dispatcher._compare_visual_effect(
            unchanged.tobytes(), unchanged.tobytes())
        changed_result, changed_reason, changed_ratio = dispatcher._compare_visual_effect(
            unchanged.tobytes(), changed.tobytes())

        self.assertIs(type(unchanged_result), bool)
        self.assertIs(type(changed_result), bool)
        self.assertIsNone(unchanged_reason)
        self.assertIsNone(changed_reason)
        self.assertEqual(unchanged_ratio, 0.0)
        self.assertGreater(changed_ratio, 0.005)
        self.assertFalse(unchanged_result)
        self.assertTrue(changed_result)
        self.assertEqual(json.loads(json.dumps({'visual_effect': changed_result})), {'visual_effect': True})

    def test_visual_effect_comparison_reports_unknown_for_invalid_or_different_frames(self):
        import cv2
        import numpy as np

        dispatcher = ActionDispatcher(mock.Mock())
        small_ok, small = cv2.imencode('.jpg', np.zeros((40, 40, 3), dtype=np.uint8))
        large_ok, large = cv2.imencode('.jpg', np.zeros((60, 40, 3), dtype=np.uint8))
        self.assertTrue(small_ok and large_ok)

        self.assertEqual(dispatcher._compare_visual_effect(b'invalid', small.tobytes()),
                         (None, 'decode_failed', None))
        self.assertEqual(dispatcher._compare_visual_effect(small.tobytes(), large.tobytes()),
                         (None, 'shape_mismatch', None))

    def test_visual_effect_is_unknown_when_optional_vision_packages_are_unavailable(self):
        original_import = builtins.__import__

        def without_vision(name, *args, **kwargs):
            if name in ('cv2', 'numpy'):
                raise ImportError('optional visual dependency unavailable')
            return original_import(name, *args, **kwargs)

        dispatcher = ActionDispatcher(mock.Mock())
        with mock.patch('builtins.__import__', side_effect=without_vision):
            result = dispatcher._compare_visual_effect(b'image', b'image')

        self.assertEqual(result, (None, 'comparison_unavailable', None))

    def test_readback_preserves_unknown_visual_effect_and_reason(self):
        runtime = mock.Mock()
        runtime.observations.observe.return_value = {
            'observation': {'observation_id': 'after', 'frames': [{'frame_id': 'after-frame'}]}}
        runtime.observations.get.return_value = SimpleNamespace(text='', elements=[])
        runtime.observations.image.side_effect = [b'before', b'after']
        dispatcher = ActionDispatcher(runtime)
        before_observation = {'observation': {'frames': [{'frame_id': 'before-frame'}]}}
        with mock.patch.object(dispatcher, '_compare_visual_effect',
                               return_value=(None, 'shape_mismatch', None)):
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
