"""A bounded apartment worker whose blocked provider cannot grow a thread pool."""
from concurrent.futures import Future, TimeoutError
import queue
import threading

from .targets import RuntimeFault


class ApartmentWorker:
    def __init__(self, name, cleanup=None):
        self.name = name
        self.cleanup = cleanup
        # Admission includes the active call, even before the apartment starts.
        # Two simultaneous callers must not race for the first queue position.
        self._queue = queue.Queue(maxsize=2)
        self._slots = threading.BoundedSemaphore(2)
        self._stopped = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self.running = False

    def _run(self):
        import pythoncom
        pythoncom.CoInitialize()
        try:
            while not self._stopped.is_set():
                try:
                    future, function, args = self._queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                if not future.set_running_or_notify_cancel():
                    self._slots.release()
                    continue
                self.running = True
                error = None
                try:
                    result = function(*args)
                except BaseException as caught:
                    error = caught
                finally:
                    self.running = False
                    self._slots.release()
                if error is None:
                    future.set_result(result)
                else:
                    future.set_exception(error)
        finally:
            while not self._queue.empty():
                future, _, _ = self._queue.get_nowait()
                future.cancel()
                self._slots.release()
            if self.cleanup:
                self.cleanup()
            pythoncom.CoUninitialize()

    def call(self, function, *args, timeout=3):
        if self._thread is not None and self._thread.ident == threading.get_ident():
            return function(*args)
        future = Future()
        with self._lock:
            if self._stopped.is_set():
                raise RuntimeFault('BACKEND_UNAVAILABLE', f'{self.name} worker has stopped')
            if not self._slots.acquire(blocking=False):
                raise RuntimeFault('BACKEND_BUSY', f'{self.name} worker still has unfinished work')
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
                self._thread.start()
            self._queue.put_nowait((future, function, args))
        try:
            return future.result(timeout=timeout)
        except TimeoutError as error:
            if future.done():
                return future.result()
            future.cancel()
            raise RuntimeFault('BACKEND_TIMEOUT', f'{self.name} collection exceeded {timeout}s. Worker running: {self.running}') from error

    def shutdown(self, timeout=1):
        self._stopped.set()
        if self._thread is not None and self._thread.ident != threading.get_ident():
            self._thread.join(timeout=timeout)
