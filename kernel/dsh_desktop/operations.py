"""Bounded execution, persistent request identities and backend input leases."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextlib import contextmanager
import hashlib
import json
import math
import threading
import time
import weakref

from . import input_gate
from .journal import OperationJournal, FINAL
from .targets import RuntimeFault


class Cancelled(RuntimeError):
    pass


class OperationContext:
    def __init__(self, manager, owner, operation_id, timeout):
        self.manager = manager
        self.owner = owner
        self.operation_id = operation_id
        self.cancelled = threading.Event()
        self.deadline = time.monotonic() + timeout
        self.token = input_gate.begin_action(timeout)
        self.backend_running = False
        self.orchestrator_done = False
        self.future = None
        self.before_dispatch = None
        self.visual_location = None

    def check(self):
        if self.cancelled.is_set():
            raise Cancelled('Operation cancelled')
        if time.monotonic() >= self.deadline:
            self.cancel()
            raise Cancelled('Operation deadline exceeded')

    def guard_input(self):
        self.check()
        refused = input_gate.guard()
        if refused:
            raise RuntimeFault('INPUT_REFUSED', str(refused['error']))

    def cancel(self):
        self.cancelled.set()
        input_gate.cancel_action(self.token)

    def change(self, **fields):
        with self.manager._changed:
            result = self.manager.journal.change(self.owner, self.operation_id, **fields)
            self.manager._changed.notify_all()
            return result

    def dispatch(self, index):
        self.guard_input()
        self.change(in_flight_step=index, dispatch='sent', uncertain_step=index)
        # Persistence can block. Recheck cancellation after the durable write.
        self.guard_input()
        if self.before_dispatch is not None:
            self.before_dispatch(index)
        self.guard_input()

    def completed(self, index, method):
        data = self.manager.journal.get(self.owner, self.operation_id)
        self.change(steps=data['steps'] + [{'index': index, 'status': 'dispatched', 'method': method}],
                    completed_steps=data['completed_steps'] + [index],
                    uncertain_step=None, in_flight_step=None)

    @contextmanager
    def hold(self, lock):
        while not lock.acquire(timeout=0.05):
            self.check()
        try:
            self.check()
            yield
        finally:
            lock.release()

    def backend(self, call, function, *, timeout=30):
        """Keep locks and the lease on the thread that can actually dispatch."""
        def run():
            with self.manager._changed:
                self.check()
                self.backend_running = True
            try:
                return input_gate.run_bound(self.token, function)
            finally:
                with self.manager._changed:
                    self.backend_running = False
                    if self.orchestrator_done:
                        self.change(worker_running=False)
                        self.manager._retire(self)
                    self.manager._changed.notify_all()
        try:
            return call(run, timeout=min(timeout, max(0.01, self.deadline - time.monotonic())))
        except TimeoutError as error:
            self.cancel()
            raise RuntimeFault('BACKEND_TIMEOUT', 'Backend exceeded its execution budget. Query the operation and observe before continuing') from error
        except BaseException:
            # Timed-out providers may return later. Cancel their lease now.
            self.cancel()
            raise


class OperationManager:
    def __init__(self, runtime):
        self.runtime = runtime
        self.journal = OperationJournal(runtime.state_dir / 'operations.sqlite3', runtime.epoch)
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='dsh-operation')
        self._slots = threading.BoundedSemaphore(64)
        self._contexts = {}
        self._targets = weakref.WeakValueDictionary()
        self._changed = threading.Condition(threading.RLock())
        self._stopped = False

    def submit(self, owner, request_key, kind, arguments, function, *, timeout=120, budget_ms=5000, cancelled=None):
        if isinstance(timeout, bool) or not isinstance(timeout, (float, int)) or not math.isfinite(timeout) or not 0 < timeout <= 120:
            raise ValueError('timeout must be finite and in (0, 120] seconds')
        self._validate_follow(budget_ms)
        digest = hashlib.sha256(json.dumps([kind, arguments], sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        with self._changed:
            if self._stopped:
                raise RuntimeFault('RUNTIME_STOPPED', 'Runtime is shutting down')
            self.journal.prune()
            data, fresh = self.journal.register(owner, request_key, digest, kind, arguments.get('target_id'))
            if fresh:
                if not self._slots.acquire(blocking=False):
                    self.journal.change(owner, data['operation_id'], status='failed', success=False,
                                        error={'code': 'OPERATION_LIMIT', 'message': 'Operation queue is full'})
                else:
                    context = OperationContext(self, owner, data['operation_id'], timeout)
                    self._contexts[data['operation_id']] = context
                    try:
                        context.future = self._pool.submit(self._run, context, function)
                    except BaseException as error:
                        context.orchestrator_done = True
                        self.journal.change(owner, context.operation_id, status='failed', success=False,
                                            error={'code': 'OPERATION_FAILED', 'message': str(error)})
                        self._retire(context)
        try:
            return self.status(owner, data['operation_id'], follow_ms=budget_ms, cancelled=cancelled)
        except Cancelled:
            self.stop(owner, data['operation_id'])
            raise

    def _run(self, context, function):
        try:
            context.check()
            context.change(status='running', worker_running=True)
            value = input_gate.run_bound(context.token, function, context)
            data = self.journal.get(context.owner, context.operation_id)
            context.change(status='completed' if value.get('success', True) else 'failed',
                           success=value.get('success', True), worker_running=False, result=value,
                           outcome=value.get('outcome', 'dispatched'), dispatch=value.get('dispatch', data['dispatch']))
        except BaseException as error:
            context.cancel()
            data = self.journal.get(context.owner, context.operation_id)
            uncertain = data.get('in_flight_step') is not None
            cancelled = isinstance(error, Cancelled)
            context.change(status='unknown' if uncertain else 'cancelled' if cancelled else 'failed',
                           success=cancelled or uncertain, worker_running=context.backend_running,
                           dispatch='unknown' if uncertain else 'partial' if data['completed_steps'] else data['dispatch'],
                           outcome='unknown' if uncertain else 'cancelled' if cancelled else 'interrupted',
                           message=str(error), next_action='observe',
                           **({} if cancelled or uncertain else
                              {'error': {'code': getattr(error, 'code', 'BACKEND_TIMEOUT' if isinstance(error, TimeoutError) else 'OPERATION_FAILED'),
                                         'message': str(error)}}))
        finally:
            with self._changed:
                context.orchestrator_done = True
                if not context.backend_running:
                    self._retire(context)
                self._changed.notify_all()

    def _retire(self, context):
        if self._contexts.pop(context.operation_id, None) is not None:
            input_gate.end_action(context.token)
            self._slots.release()
        if self._stopped and not self._contexts:
            self.journal.close()

    def target_lock(self, target):
        with self._changed:
            # Frames and owners of the same real tab share one lock.
            identity = target.identity[0] if target.kind == 'browser' else target.identity
            key = (target.kind, identity)
            lock = self._targets.get(key)
            if lock is None:
                lock = threading.Lock()
                self._targets[key] = lock
            return lock

    @staticmethod
    def _validate_follow(follow_ms):
        if not isinstance(follow_ms, int) or isinstance(follow_ms, bool) or not 0 <= follow_ms <= 10000:
            raise ValueError('follow_ms must be an integer from 0 to 10000')

    def status(self, owner, operation_id=None, *, request_key=None, follow_ms=0, cancelled=None):
        self._validate_follow(follow_ms)
        deadline = time.monotonic() + follow_ms / 1000
        with self._changed:
            self.journal.prune()
            data = self.journal.get(owner, operation_id, request_key=request_key)
            while data['status'] not in FINAL and time.monotonic() < deadline:
                if cancelled is not None and cancelled.is_set():
                    raise Cancelled('Status follow cancelled')
                self._changed.wait(min(0.05, max(0, deadline - time.monotonic())))
                data = self.journal.get(owner, operation_id, request_key=request_key)
        result = data.pop('result', {})
        return {**result, **data}

    def stop(self, owner, operation_id=None):
        with self._changed:
            records = [self.journal.get(owner, operation_id)] if operation_id else self.journal.active(owner)
            stopped = []
            for data in records:
                context = self._contexts.get(data['operation_id'])
                if context is not None:
                    self._cancel_context(context, data)
                    stopped.append(self.journal.get(owner, context.operation_id))
            return {'success': True, 'operations': stopped}

    def _cancel_context(self, context, data):
        context.cancel()
        if context.future is not None and context.future.cancel():
            context.change(status='cancelled', outcome='cancelled', worker_running=False)
            context.orchestrator_done = True
            self._retire(context)
        elif data['status'] not in FINAL:
            context.change(status='cancelling', outcome='cancelled')

    def shutdown(self):
        with self._changed:
            if self._stopped:
                return
            self._stopped = True
            for context in list(self._contexts.values()):
                self._cancel_context(context, self.journal.get(context.owner, context.operation_id))
        self._pool.shutdown(wait=False, cancel_futures=False)
        deadline = time.monotonic() + 2
        with self._changed:
            while self._contexts and time.monotonic() < deadline:
                self._changed.wait(0.05)
            if not self._contexts:
                self.journal.close()
